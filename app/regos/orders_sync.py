"""Обратная синхронизация: статус заказа из REGOS в витрину.

Выгрузка (orders_push.py) доводит заказ до «Утверждён» и останавливается.
Дальше он живёт в REGOS своей жизнью: кассир принимает его в работу, пробивает
оплату, закрывает или отменяет. Раньше об этом знала только касса — покупатель
в приложении видел «Принят» до тех пор, пока оператор не поменяет состояние
руками. Здесь мы это забираем обратно.

Как устроено. Раз в минуту берём заказы, которые уже выгружены и ещё не
завершились, и спрашиваем REGOS об их документах одним запросом. Фильтр
`id In` принимает список только строкой через запятую — список массивом
отклоняется ошибкой 1008. Дороже одного вызова в минуту это не обходится,
и лимит REGOS (2 запроса в секунду) остаётся свободным для каталога.

Двигаем только вперёд. В REGOS документ может вернуться в «Новый» — так уже
бывало с половиной выгруженных заказов. Если бы мы повторяли это движение,
покупатель, которому сказали «собираем», увидел бы «ждёт оплаты», а за ним
и второе сообщение об этом. Поэтому состояние меняется, только если новое
дальше нынешнего; исключение — отмена, она приходит из любого места.

Об отмене, пришедшей с кассы, сообщаем сотрудникам: заказ мог быть уже
в сборке. Остальные переходы они и так видят в REGOS.
"""
import logging
import time

from sqlalchemy import select

from app import notify, order_status
from app.models import Order
from app.regos import config
from app.regos.client import RegosClient, RegosError

log = logging.getLogger(__name__)

# Статусы документа DocOrderDelivery в REGOS -> состояния витрины.
# Значения взяты с живого аккаунта (поле status: id, name, name_var):
#   22 Новый             CTLG_ORDER_STATUS_NEW           до подтверждения
#   23 Утвержден         CTLG_ORDER_STATUS_APPROVED      ждёт приёма кассиром
#   24 В обработке       CTLG_ORDER_STATUS_PROCESSING    кассир принял, собирают
#   25 Оплата (продажа)  CTLG_ORDER_STATUS_PREPARE_PAY   пробивают на кассе
#   26 Завершен                                          закрыт
#   27 Отменен           CTLG_ORDER_STATUS_CANCELED      отменён
# 22 сюда не входит намеренно: это состояние до подтверждения, и возвращать
# в него заказ, о котором покупателю уже сказали «собираем», нельзя.
STATUS = {
    23: "CONFIRMED",
    24: "ASSEMBLING",
    25: "DELIVERING",
    26: "DONE",
    27: "CANCELED",
}

# Порядок состояний витрины. Нужен, чтобы отличать движение вперёд от возврата
# назад; отмена вне этого ряда — она приходит откуда угодно.
RANK = {"NEW": 0, "CONFIRMED": 1, "ASSEMBLING": 2, "DELIVERING": 3, "DONE": 4}

# Сколько документов спрашиваем одним вызовом. REGOS ограничивает длину строки
# фильтра, а не количество: сотня идентификаторов — это около 400 символов.
CHUNK = 100

# Как часто смотрим. Минуты хватает: заказ на кассе не меняется чаще, а лимит
# запросов REGOS общий с синхронизацией каталога.
INTERVAL_SECONDS = 60


def watched(db) -> list[Order]:
    """Заказы, за которыми есть смысл следить: выгруженные и незакрытые."""
    return list(db.scalars(
        select(Order)
        .where(
            Order.regos_document_id.is_not(None),
            Order.status.notin_(order_status.FINAL),
        )
        .order_by(Order.id)
    ))


def fetch_statuses(client: RegosClient, document_ids: list[int]) -> dict[int, dict]:
    """id документа -> его статус ({'id': 24, 'name': 'В обработке', …}).

    Фильтр `id` принимает только Equal и In (проверено, остальные операторы
    отклоняются с ошибкой 1008), а значение In — строкой через запятую:
    массив тот же метод не принимает.
    """
    found: dict[int, dict] = {}
    for start in range(0, len(document_ids), CHUNK):
        chunk = document_ids[start:start + CHUNK]
        result = client.call("DocOrderDelivery/Get", {
            "filters": [{
                "Field": "id",
                "Operator": "In",
                "Value": ",".join(str(i) for i in chunk),
            }],
            "limit": len(chunk),
            "offset": 0,
        })
        rows = result.get("result") if isinstance(result, dict) else result
        for row in rows or []:
            status = row.get("status") or {}
            if isinstance(status, dict) and status.get("id"):
                found[row["id"]] = status
    return found


def target_status(order: Order, regos_status: dict) -> str | None:
    """Во что перевести заказ. None — оставить как есть."""
    code = regos_status.get("id")
    new = STATUS.get(code)
    if new is None:
        if code != 22:              # 22 «Новый» — ожидаемое состояние, не шумим
            log.info("Заказ %s: статус REGOS %s (%s) витрине неизвестен",
                     order.number, code, regos_status.get("name"))
        return None
    if new == order.status:
        return None
    if new == "CANCELED":
        return new                  # отмена приходит из любого состояния
    if order.status == "NEW" and order.payment_method == "online" and order.paid_at is None:
        # Неоплаченный заказ картой в REGOS попасть не должен вовсе. Если всё же
        # попал, «Принят» от кассы не превращает его в оплаченный: подтверждает
        # такой заказ только уведомление об оплате
        log.warning("Заказ %s: касса продвинула неоплаченный заказ картой (%s) — не принимаем",
                    order.number, regos_status.get("name"))
        return None
    if RANK.get(new, -1) <= RANK.get(order.status, -1):
        # возврат назад: в REGOS такое бывает, покупателю показывать нельзя
        return None
    return new


def sync(db, client: RegosClient | None = None, notify_staff: bool = True) -> dict:
    """Один проход. Возвращает, что проверили и что изменилось.

    notify_staff=False нужен для первого прогона на уже накопившихся заказах:
    там расхождение копилось днями, и рассылать по нему сообщения задним
    числом незачем.
    """
    orders = watched(db)
    if not orders:
        return {"checked": 0, "changed": []}

    client = client or RegosClient()
    statuses = fetch_statuses(client, [o.regos_document_id for o in orders])

    changed = []
    for order in orders:
        status = statuses.get(order.regos_document_id)
        if status is None:
            # документ не найден: его могли удалить в REGOS. Заказ не трогаем —
            # витрина не должна терять заказ из-за чужой уборки
            log.info("Заказ %s: документ %s в REGOS не найден",
                     order.number, order.regos_document_id)
            continue
        new = target_status(order, status)
        if new is None:
            continue
        was = order.status
        # условно: пока ждали ответа REGOS, заказ мог поменять оператор или оплата
        if not order_status.move(db, order, new):
            continue
        # отмена на кассе — единственное, о чём сотрудникам надо сказать:
        # остальные переходы они и так видят в REGOS
        if notify_staff and new == "CANCELED":
            notify.queue_staff(db, order, notify.staff_canceled(order, "на кассе"))
        changed.append({"number": order.number, "from": was, "to": new,
                        "regos": status.get("name")})
        log.info("Заказ %s: %s -> %s (REGOS: %s)",
                 order.number, was, new, status.get("name"))

    if changed:
        db.commit()
    return {"checked": len(orders), "changed": changed}


def sync_once(notify_staff: bool = True) -> dict:
    """Проход со своей сессией базы — для фонового потока и для скрипта."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return sync(db, notify_staff=notify_staff)
    finally:
        db.close()


def enabled() -> bool:
    """Синхронизация статусов требует только ключа: она ничего не создаёт
    в REGOS, а лишь читает документы, которые мы туда уже отправили."""
    return config.is_configured()


def worker() -> None:
    while True:
        try:
            sync_once()
        except (RegosError, RuntimeError, OSError) as exc:
            # недоступный REGOS — обычное дело: ждём следующего круга
            log.warning("Статусы заказов не забрали: %s", exc)
        except Exception:                       # noqa: BLE001
            # поток обязан пережить что угодно: иначе статусы тихо перестанут
            # обновляться, а служба останется живой, и никто не заметит
            log.exception("Сбой синхронизации статусов заказов")
        time.sleep(INTERVAL_SECONDS)


_started = False


def start_worker() -> None:
    """Запускается при старте приложения — рядом с потоком уведомлений."""
    global _started
    import threading

    if _started or not enabled():
        return
    _started = True
    threading.Thread(target=worker, name="regos-status", daemon=True).start()
    log.info("Синхронизация статусов заказов с REGOS включена")
