"""Служебный бот магазина: заказы в Telegram сотрудникам.

Бот не покупательский. Он рассказывает магазину о заказах: пришёл новый —
карточка с составом, контактами и адресом; отменили — сообщение об этом.
Покупатель следит за своим заказом в самом приложении, в «Моих заказах».

Отсюда и устройство доступа: в рассылку попадает только чат, которому его
выдали в админ-панели (см. app/bot.py). В карточке телефон и адрес человека,
и подписаться на такое по собственному желанию нельзя.

Сообщения кладутся в очередь и уходят отдельным потоком. Отправлять прямо
из обработчика запроса нельзя: недоступный Telegram задерживал бы ответ
покупателю, а перезапуск службы посреди отправки терял бы сообщение молча.
"""
import html
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

from sqlalchemy import select

from app.models import Notification, Order, StaffChat, utcnow

log = logging.getLogger(__name__)

TOKEN = os.getenv("NOTIFY_BOT_TOKEN", "").strip()

# Имя бота нужно приложению для ссылки t.me/<имя>?start=…. Берём его у самого
# Telegram, чтобы не заводить ещё одну переменную окружения, которую однажды
# забудут поправить после смены бота. Переменная всё же есть — на случай,
# когда при старте нет связи.
USERNAME = os.getenv("NOTIFY_BOT_USERNAME", "").strip().lstrip("@")

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20

# Сколько раз пробуем отправить, прежде чем сдаться. Перегрузка Telegram
# и обрыв связи лечатся повтором, всё остальное повтором не лечится.
MAX_ATTEMPTS = 5

# Сообщение о заказе имеет смысл, пока заказ свеж. Чат, которому выдали доступ
# через неделю, не должен получить ворох старых заказов.
MAX_AGE = timedelta(hours=24)

# Как часто поток разбирает очередь. Свежие сообщения уходят сразу — их
# отправляет фоновая задача запроса; этот проход нужен для повторов и для чатов,
# которым доступ выдали уже после оформления заказа.
SWEEP_SECONDS = 30


class NotifyError(Exception):
    """Отправить не удалось. permanent=True — повторять бессмысленно."""

    def __init__(self, message: str, permanent: bool = False, retry_after: int = 0):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


def enabled() -> bool:
    return bool(TOKEN)


def call(method: str, payload: dict) -> dict:
    """Запрос к Bot API. Возвращает result, на отказ поднимает NotifyError.

    Telegram отвечает содержательной ошибкой в теле и ставит код состояния
    4xx, из-за которого urllib поднимает HTTPError раньше, чем мы прочитаем
    описание. Поэтому тело читается и в этом случае: без него в журнале
    остаётся голое «HTTP Error 403: Forbidden», по которому непонятно,
    заблокировал ли покупатель бота или неверен токен.
    """
    request = urllib.request.Request(
        API.format(token=TOKEN, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (ValueError, OSError):
            raise NotifyError(f"{method}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # сеть или недоступный Telegram — тот случай, когда повтор помогает
        raise NotifyError(f"{method}: нет связи с Telegram ({exc})") from exc

    if body.get("ok"):
        return body.get("result", {})

    code = body.get("error_code")
    description = body.get("description", "")
    retry_after = int((body.get("parameters") or {}).get("retry_after", 0))
    # 403 — покупатель не нажимал «Старт» или заблокировал бота; 400 «chat not
    # found» — чата нет вовсе. Ни то, ни другое не исправится повтором
    permanent = code == 403 or (code == 400 and "chat not found" in description.lower())
    raise NotifyError(f"{method}: {code} {description}", permanent, retry_after)


def bot_username() -> str:
    """Имя бота для ссылки. Спрашивается один раз и запоминается."""
    global USERNAME
    if USERNAME or not TOKEN:
        return USERNAME
    try:
        USERNAME = call("getMe", {}).get("username", "")
    except NotifyError as exc:
        log.warning("Не удалось узнать имя информационного бота: %s", exc)
    return USERNAME


def send_message(chat_id: int, text: str) -> None:
    call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


# ------------------------------------------------------------------ тексты


def money(value: int) -> str:
    """125000 -> «125 000 сум». Пробелы неразрывные, чтобы сумма не ломалась."""
    return f"{value:,}".replace(",", " ") + " сум"


def esc(text: str) -> str:
    """Экранирование: в имени товара и в адресе встречается «&», а разметка
    сообщения — HTML, и такой текст Telegram отвергает целиком."""
    return html.escape(text or "")


# Длинный заказ не режем на части, но и список из полусотни строк в сообщении
# нечитаем: показываем начало и говорим, сколько осталось.
ITEMS_SHOWN = 15


# --------------------------------------------------------- сообщения магазину

# Карточка заказа для сотрудников. Здесь, в отличие от сообщения покупателю,
# нужны контакты и адрес: по ним заказ собирают и везут. Поэтому подписка
# на эти сообщения закрыта кодом — см. app/bot.py.
PAYMENT_TEXT = {"cash": "наличными курьеру", "online": "онлайн картой"}


def map_link(order: Order) -> str | None:
    """Ссылка на точку, которую покупатель поставил на карте.

    Адрес строкой курьеру всё равно нужен — по нему подъезд и квартира,
    — а вот дом он находит по этой ссылке. Открывается тем приложением,
    которое у него стоит: Google и Яндекс.Карты понимают такую ссылку оба.
    """
    if order.lat is None or order.lon is None:
        return None
    return f"https://maps.google.com/?q={order.lat},{order.lon}"


def staff_new_order(order: Order) -> str:
    lines = [
        f"🆕 Новый заказ <b>{esc(order.number)}</b> — {money(order.total)}",
        "",
    ]
    for item in order.items[:ITEMS_SHOWN]:
        count = f" × {item.quantity}" if item.quantity > 1 else ""
        lines.append(f"• {esc(item.product_name)} {esc(item.weight)}{count} — "
                     f"{money(item.price * item.quantity)}")
    if len(order.items) > ITEMS_SHOWN:
        lines.append(f"…и ещё {len(order.items) - ITEMS_SHOWN}")

    contact = [esc(order.customer_name), esc(order.phone)]
    if order.telegram_username:
        contact.append(f"@{esc(order.telegram_username)}")
    lines += [
        "",
        " · ".join(contact),
        f"📍 {esc(order.address)}"
        + (f' — <a href="{map_link(order)}">точка на карте</a>' if map_link(order) else ""),
        f"🕐 {esc(order.delivery_slot)}",
        f"💳 {PAYMENT_TEXT.get(order.payment_method, esc(order.payment_method))}",
    ]
    if order.comment:
        lines.append(f"📝 {esc(order.comment)}")
    return "\n".join(lines)


def staff_canceled(order: Order, source: str) -> str:
    """source — откуда пришла отмена: «в панели», «из REGOS». Сотруднику важно
    знать, чьё это действие: своё или кассы."""
    return (f"❌ Заказ <b>{esc(order.number)}</b> отменён ({esc(source)})\n"
            f"{esc(order.customer_name)} · {esc(order.phone)} · {money(order.total)}")


def staff_chats(db) -> list[StaffChat]:
    return list(db.scalars(select(StaffChat).where(StaffChat.active)))


def queue_staff(db, order: Order, text: str) -> list[Notification]:
    """Кладёт сообщение во все чаты сотрудников. Коммит — на вызывающем коде."""
    if not enabled():
        return []
    rows = []
    for chat in staff_chats(db):
        row = Notification(
            # для сообщения сотрудникам адресат известен сразу: это сам чат,
            # искать его по подписке покупателя не нужно
            telegram_id=chat.chat_id, chat_id=chat.chat_id,
            order_id=order.id, kind="staff", text=text,
        )
        db.add(row)
        rows.append(row)
    return rows


def send_many(notification_ids: list[int]) -> None:
    """Фоновая отправка нескольких сообщений — обычно это рассылка сотрудникам."""
    for notification_id in notification_ids:
        send_one(notification_id)


# ------------------------------------------------------------------ очередь


def deliver(db, row: Notification) -> bool:
    """Пытается отправить одно сообщение. True — ушло."""
    # Доступ проверяем в момент отправки, а не когда клали в очередь: если его
    # успели отозвать, чат не должен получить заказ, «уже выписанный» на него
    chat = db.get(StaffChat, row.chat_id)
    if chat is None or not chat.active:
        return False

    try:
        send_message(chat.chat_id, row.text)
    except NotifyError as exc:
        row.error = str(exc)[:500]
        if exc.permanent:
            # бота заблокировали или выгнали из группы: слать туда больше
            # некуда, пока чат не включат заново
            chat.active = False
            log.info("Чат %s недоступен для бота: %s", chat.chat_id, exc)
        else:
            row.attempts += 1
            log.warning("Сообщение %s не ушло: %s", row.id, exc)
        return False

    row.sent_at = utcnow()
    row.error = None
    return True


def send_one(notification_id: int) -> None:
    """Отправка одного сообщения фоном, сразу после события.

    Своя сессия базы: та, в которой создавался заказ, к этому моменту закрыта.
    Ошибки наружу не выпускаем — заказ уже оформлен, и молчащий Telegram
    не повод отвечать покупателю отказом. Что не ушло, доберёт поток очереди.
    """
    from app.db import SessionLocal

    if not enabled():
        return
    db = SessionLocal()
    try:
        row = db.get(Notification, notification_id)
        if row is not None and row.sent_at is None:
            deliver(db, row)
            db.commit()
    except Exception:                       # noqa: BLE001
        log.exception("Сбой отправки сообщения %s", notification_id)
    finally:
        db.close()


def pending(db) -> list[Notification]:
    """Что ещё не ушло и что ещё имеет смысл отправлять."""
    return list(db.scalars(
        select(Notification)
        .where(
            Notification.sent_at.is_(None),
            Notification.attempts < MAX_ATTEMPTS,
            Notification.created_at > utcnow() - MAX_AGE,
        )
        .order_by(Notification.id)
        .limit(100)
    ))


def sweep() -> int:
    """Проход по очереди. Возвращает, сколько сообщений ушло."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        rows = pending(db)
        sent = 0
        for row in rows:
            if deliver(db, row):
                sent += 1
                # Telegram разрешает около сообщения в секунду на чат и 30
                # в секунду всего; рассылок у нас нет, но пауза дешевле, чем
                # разбираться потом с ошибкой 429
                time.sleep(0.05)
        if rows:
            db.commit()
        return sent
    finally:
        db.close()


def worker() -> None:
    while True:
        try:
            sweep()
        except Exception:                   # noqa: BLE001
            # поток обязан пережить любую ошибку: иначе уведомления тихо
            # прекратятся, а служба останется живой, и никто не заметит
            log.exception("Сбой разбора очереди уведомлений")
        time.sleep(SWEEP_SECONDS)


_started = False


def start_worker() -> None:
    """Запускается при старте приложения.

    Поток, а не задача asyncio: внутри обычные синхронные обращения к базе
    и к Telegram, и блокировать ими цикл событий, из которого отвечают
    покупателю, незачем.
    """
    global _started
    if _started or not enabled():
        return
    _started = True
    threading.Thread(target=worker, name="notify", daemon=True).start()
    log.info("Информационный бот включён")
