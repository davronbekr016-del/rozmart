"""Вебхук магазинного бота — только для оплаты.

Магазинный бот `@rozmartuz_bot` открывает приложение и выставляет счета.
Telegram сообщает ему о платежах двумя обновлениями:

* `pre_checkout_query` — «можно ли списать деньги». Ответить надо за 10 секунд,
  иначе Telegram отменит платёж сам;
* `successful_payment` — деньги списаны. Приходит сервисным сообщением в чат
  покупателя с ботом.

Со служебным ботом (app/bot.py) не смешиваем: у них разные токены, разные
адреса вебхука и разные секреты. Перепутай их — и уведомление об оплате уйдёт
в обработчик, который его не ждёт.

Всё прочее, что пишут магазинному боту, пропускаем молча: до этого вебхука
у него не было вовсе, и на сообщения он не отвечал — так и оставляем.
"""
import hmac
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import notify, payments
from app.db import get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["Магазинный бот"])

SECRET = os.getenv("SHOP_WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/tg/shop"


def handle_pre_checkout(db: Session, query: dict) -> dict:
    """Отвечает, можно ли списывать.

    Ответ уходит прямо в теле ответа на вебхук — Telegram умеет принимать так
    вызов метода. Отдельный запрос к api.telegram.org занял бы лишнюю секунду
    из десяти, отведённых на ответ, а медленный Telegram съел бы их все.

    Молчать нельзя: без ответа за 10 секунд Telegram отменит платёж сам,
    поэтому любой сбой проверки — это «нет» с понятным текстом, а не ошибка.
    """
    try:
        ok, error = payments.check_pre_checkout(db, query)
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Не проверили заказ перед оплатой")
        ok, error = False, "Не получилось проверить заказ. Попробуйте ещё раз через минуту."
    answer = {"method": "answerPreCheckoutQuery",
              "pre_checkout_query_id": query.get("id"), "ok": ok}
    if not ok:
        answer["error_message"] = error
    return answer


def alert_staff(db: Session, background: BackgroundTasks, order, text: str) -> None:
    """Сообщение сотрудникам о том, что с оплатой что-то не так."""
    if order is None:
        # заказа нет — привязать сообщение не к чему, шлём как есть
        from app.models import Notification
        rows = []
        for chat in notify.staff_chats(db):
            row = Notification(telegram_id=chat.chat_id, chat_id=chat.chat_id,
                               order_id=None, kind="staff", text=text)
            db.add(row)
            rows.append(row)
    else:
        rows = notify.queue_staff(db, order, text)
    if rows:
        db.commit()
        background.add_task(notify.send_many, [row.id for row in rows])


def send_onward(db: Session, background: BackgroundTasks, order, only_missing=False) -> None:
    """Оплаченный заказ — дальше тем же путём, что наличный: сотрудникам
    (теперь с пометкой «оплачено») и в REGOS.

    only_missing — при повторе уведомления: сообщение ставим, только если его
    ещё нет. Выгрузку запускаем в любом случае — push_one сам проверяет,
    выгружен ли заказ.
    """
    from app.models import Notification
    from app.regos.orders_push import push_one

    sent = only_missing and db.query(Notification).filter(
        Notification.order_id == order.id, Notification.kind == "staff").first()
    if not sent:
        rows = notify.queue_staff(db, order, notify.staff_new_order(order))
        if rows:
            db.commit()
            background.add_task(notify.send_many, [row.id for row in rows])
    background.add_task(push_one, order.id)


def handle_payment(db: Session, background: BackgroundTasks, payment: dict) -> None:
    """Проводит платёж и запускает заказ дальше тем же путём, что наличный.

    Исключения отсюда наружу выпускаем намеренно: вебхук ответит 500, и Telegram
    пришлёт уведомление ещё раз. Проглотить ошибку — значит потерять платёж:
    деньги списаны, а заказ через полчаса отменит автоотмена. Повтор безопасен —
    второй раз платёж не проведёт уникальный payment_charge_id.
    """
    try:
        outcome, order = payments.record_payment(db, payment)
    except IntegrityError:
        # то же уведомление пришло дважды одновременно: второе упёрлось
        # в уникальный индекс, платёж уже проведён первым
        db.rollback()
        log.info("Повтор уведомления об оплате %s", payment.get("telegram_payment_charge_id"))
        return

    charge = payment.get("telegram_payment_charge_id") or "—"
    amount = notify.money((payment.get("total_amount") or 0) // payments.MINOR)

    if outcome == "confirmed":
        send_onward(db, background, order)
        return
    if outcome == "duplicate":
        # Повтор уведомления. Обычно делать нечего, но повтор бывает и потому,
        # что в прошлый раз мы ответили 500: платёж тогда уже записался, а
        # сообщение сотрудникам или выгрузка не успели. Долечиваем — оба шага
        # безопасно повторять
        if order is not None and order.status == "CONFIRMED" and order.paid_at:
            send_onward(db, background, order, only_missing=True)
        return
    if outcome == "late":
        state = "уже оплачен" if order.paid_at else "отменён"
        alert_staff(db, background, order, (
            f"⚠️ Оплата картой по заказу <b>{notify.esc(order.number)}</b>, "
            f"а заказ {state}.\n"
            f"Списано {amount}, платёж {notify.esc(charge)}.\n"
            "Нужен возврат покупателю или восстановление заказа — решите вручную."
        ))
        return
    alert_staff(db, background, None, (
        f"⚠️ Оплата картой по неизвестному заказу "
        f"«{notify.esc(payment.get('invoice_payload') or '')}».\n"
        f"Списано {amount}, платёж {notify.esc(charge)}. Нужен возврат."
    ))


def process(db: Session, background: BackgroundTasks, update: dict) -> dict:
    """Разбор обновления. Синхронный: внутри запросы к базе, и держать ими
    цикл событий, из которого отвечают покупателям, нельзя."""
    if "pre_checkout_query" in update:
        return handle_pre_checkout(db, update["pre_checkout_query"])
    payment = (update.get("message") or {}).get("successful_payment")
    if payment:
        handle_payment(db, background, payment)
    return {"ok": True}


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def shop_webhook(
    request: Request,
    background: BackgroundTasks,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
):
    """Обновления магазинного бота."""
    # сравнение постоянного времени: обычное «!=» по времени ответа выдаёт,
    # сколько первых символов секрета угадано
    if not SECRET or not hmac.compare_digest((secret or "").encode(), SECRET.encode()):
        log.warning("Обновление магазинного бота с неверным секретом отклонено")
        return {"ok": False}
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}

    try:
        return await run_in_threadpool(process, db, background, update)
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Не разобрали обновление магазинного бота — Telegram повторит")
        # 500 — просьба повторить. Для уведомления об оплате это единственный
        # способ его не потерять
        return JSONResponse({"ok": False}, status_code=500)
