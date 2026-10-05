"""Бот доставщиков.

Доставщик видит два раздела — кнопки под полем ввода:

* «📦 Заказы» — свободные заказы, каждый отдельным сообщением с кнопкой
  «Принять заказ». Принял — заказ его и переходит в «В доставке»;
* «🚚 Доставка» — его заказы в пути. У каждого: «Завершить» (покупатель
  получил заказ — «Выполнен»), «Отменить» (отказаться: заказ возвращается
  в прежнее состояние и снова виден всем) и «Открыть на карте».

Взять заказ может только один доставщик. Двое нажимают «Принять» в одну
секунду — оба запроса читают заказ свободным, но запись условная
(`UPDATE … WHERE courier_id IS NULL`), и второй видит ноль изменённых
строк: ему бот отвечает, что заказ уже взяли.

Что считается свободным: принятый магазином заказ без доставщика —
«Принят», «Собирается» и «В доставке» (касса уже провела продажу, а курьера
ещё нет). Неоплаченный заказ картой сюда не попадает: он в «Ждёт оплаты».

Доступ — как у служебного бота: писать боту может кто угодно, но заказы
с телефоном и адресом покупателя видит только тот, кого администратор
включил в панели («Состав каталога» → «Доставщики»).

Вебхук — /tg/courier, свой токен и свой секрет:
`python -m scripts.setup_courier_bot`.
"""
import hmac
import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app import notify
from app.db import get_db
from app.models import Courier, Order, utcnow

log = logging.getLogger(__name__)

router = APIRouter(tags=["Бот доставщиков"])

TOKEN = os.getenv("COURIER_BOT_TOKEN", "").strip()
SECRET = os.getenv("COURIER_WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/tg/courier"

BTN_ORDERS = "📦 Заказы"
BTN_DELIVERY = "🚚 Доставка"
KEYBOARD = {
    "keyboard": [[{"text": BTN_ORDERS}, {"text": BTN_DELIVERY}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

# какие заказы можно взять: принятые магазином и ещё без доставщика
AVAILABLE = ("CONFIRMED", "ASSEMBLING", "DELIVERING")
# сколько заказов показывать за раз: каждый — отдельное сообщение
LIST_LIMIT = 15
ITEMS_SHOWN = 10

WELCOME = ("Это бот доставщиков ROZMART.\n\n"
           f"«{BTN_ORDERS}» — свободные заказы, их можно принять.\n"
           f"«{BTN_DELIVERY}» — ваши заказы в пути.")
PENDING = ("Это бот доставщиков ROZMART.\n\n"
           "Я вас запомнил. Чтобы видеть заказы, попросите администратора "
           "включить вас в панели: «Состав каталога» → «Доставщики».")


def enabled() -> bool:
    return bool(TOKEN)


def call(method: str, payload: dict) -> dict:
    return notify.call(method, payload, token=TOKEN)


def safe_call(method: str, payload: dict) -> None:
    """Ответ человеку: не дошёл — не повод ронять обработку."""
    try:
        call(method, payload)
    except notify.NotifyError as exc:
        log.warning("Бот доставщиков: %s не выполнен: %s", method, exc)


def send(chat_id: int, text: str, markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if markup is not None:
        payload["reply_markup"] = markup
    safe_call("sendMessage", payload)


# ------------------------------------------------------------------ карточка

def map_url(order: Order) -> str:
    """Карта: по точке, если покупатель её поставил, иначе поиск по адресу."""
    if order.lat is not None and order.lon is not None:
        return f"https://maps.google.com/?q={order.lat},{order.lon}"
    return "https://maps.google.com/?q=" + quote(order.address)


def card(order: Order) -> str:
    """Всё, что нужно доставщику: деньги, кому, куда, когда и что везти."""
    if order.paid_at is not None:
        money = "💳 <b>ОПЛАЧЕНО КАРТОЙ — деньги не брать</b>"
    else:
        money = f"💵 Получить с покупателя: <b>{notify.money(order.total)}</b>"
    lines = [f"📦 <b>{notify.esc(order.number)}</b> — {notify.money(order.total)}", money, "",
             f"👤 {notify.esc(order.customer_name)} · {notify.esc(order.phone)}"]
    if order.telegram_username:
        lines[-1] += f" · @{notify.esc(order.telegram_username)}"
    lines += [f"📍 {notify.esc(order.address)}", f"🕐 {notify.esc(order.delivery_slot)}"]
    if order.comment:
        lines.append(f"📝 {notify.esc(order.comment)}")
    lines.append("")
    for item in order.items[:ITEMS_SHOWN]:
        count = f" × {item.quantity}" if item.quantity > 1 else ""
        lines.append(f"• {notify.esc(item.product_name)} {notify.esc(item.weight)}{count}")
    if len(order.items) > ITEMS_SHOWN:
        lines.append(f"…и ещё {len(order.items) - ITEMS_SHOWN}")
    return "\n".join(lines)


def take_markup(order: Order) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Принять заказ", "callback_data": f"take:{order.id}"}]]}


def delivery_markup(order: Order) -> dict:
    return {"inline_keyboard": [
        [{"text": "✔️ Завершить", "callback_data": f"done:{order.id}"},
         {"text": "↩️ Отменить", "callback_data": f"drop:{order.id}"}],
        [{"text": "📍 Открыть на карте", "url": map_url(order)}],
    ]}


def confirm_markup(order: Order, action: str) -> dict:
    yes = "Да, заказ доставлен" if action == "done" else "Да, отказаться от заказа"
    return {"inline_keyboard": [
        [{"text": yes, "callback_data": f"{action}!:{order.id}"}],
        [{"text": "Назад", "callback_data": f"back:{order.id}"}],
    ]}


# ------------------------------------------------------------------ списки

def courier_of(db: Session, telegram_id: int) -> Courier | None:
    courier = db.get(Courier, telegram_id)
    return courier if courier is not None and courier.active else None


def available(db: Session) -> list[Order]:
    return list(db.scalars(
        select(Order).where(Order.status.in_(AVAILABLE), Order.courier_id.is_(None))
        .order_by(Order.created_at).limit(LIST_LIMIT)
        .options(selectinload(Order.items))
    ))


def mine(db: Session, telegram_id: int) -> list[Order]:
    return list(db.scalars(
        select(Order).where(Order.courier_id == telegram_id, Order.status == "DELIVERING")
        .order_by(Order.courier_taken_at).options(selectinload(Order.items))
    ))


def show_orders(db: Session, chat_id: int) -> None:
    orders = available(db)
    if not orders:
        send(chat_id, "Свободных заказов нет.", KEYBOARD)
        return
    send(chat_id, f"Свободные заказы: {len(orders)}", KEYBOARD)
    for order in orders:
        send(chat_id, card(order), take_markup(order))


def show_delivery(db: Session, chat_id: int) -> None:
    orders = mine(db, chat_id)
    if not orders:
        send(chat_id, f"У вас нет заказов в доставке. Взять — в «{BTN_ORDERS}».", KEYBOARD)
        return
    send(chat_id, f"Ваши заказы в доставке: {len(orders)}", KEYBOARD)
    for order in orders:
        send(chat_id, card(order), delivery_markup(order))


# ------------------------------------------------------------------ действия

def take(db: Session, order_id: int, courier: Courier) -> bool:
    """Захват заказа. Условная запись: возьмёт только первый."""
    changed = db.execute(
        update(Order).where(Order.id == order_id, Order.courier_id.is_(None),
                            Order.status.in_(AVAILABLE))
        # в SET справа — значения до изменения: прежнее состояние запоминается
        .values(courier_id=courier.telegram_id, courier_name=courier.name,
                courier_taken_at=utcnow(), courier_prev_status=Order.status,
                status="DELIVERING")
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return changed == 1


def finish(db: Session, order_id: int, courier: Courier) -> bool:
    """Покупатель получил заказ — «Выполнен». Только свой и только в пути."""
    changed = db.execute(
        update(Order).where(Order.id == order_id, Order.courier_id == courier.telegram_id,
                            Order.status == "DELIVERING")
        .values(status="DONE", delivered_at=utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return changed == 1


def release(db: Session, order_id: int, courier_id: int) -> bool:
    """Доставщик отказался: заказ свободен и в прежнем состоянии."""
    order = db.get(Order, order_id)
    if order is None:
        return False
    back = order.courier_prev_status if order.courier_prev_status in AVAILABLE else "ASSEMBLING"
    changed = db.execute(
        update(Order).where(Order.id == order_id, Order.courier_id == courier_id,
                            Order.status == "DELIVERING")
        .values(status=back, courier_id=None, courier_name=None,
                courier_taken_at=None, courier_prev_status=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return changed == 1


def release_all(db: Session, courier_id: int) -> list[str]:
    """Все заказы доставщика — обратно свободными. Нужно, когда доступ
    отключают: иначе его заказы так и висели бы «в доставке» ни у кого."""
    released = []
    for order in mine(db, courier_id):
        if release(db, order.id, courier_id):
            released.append(order.number)
    return released


def staff(db: Session, background: BackgroundTasks | None, order: Order, text: str) -> None:
    """Сообщение сотрудникам в служебный бот: кто взял, кто доставил."""
    rows = notify.queue_staff(db, order, text)
    if not rows:
        return
    db.commit()
    ids = [row.id for row in rows]
    if background is not None:
        background.add_task(notify.send_many, ids)
    else:
        notify.send_many(ids)


def order_canceled(order: Order) -> None:
    """Заказ отменили в панели или на кассе, а он у доставщика в пути —
    сказать ему, чтобы не вёз."""
    if not enabled() or not order.courier_id:
        return
    send(order.courier_id, f"❌ Заказ <b>{notify.esc(order.number)}</b> отменён — не везите.\n"
                           f"{notify.esc(order.customer_name)} · {notify.esc(order.address)}")


# ------------------------------------------------------------------ обработка

def edit(chat_id: int, message_id: int, text: str, markup: dict | None) -> None:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup is not None:
        payload["reply_markup"] = markup
    safe_call("editMessageText", payload)


def answer(callback_id: str, text: str = "", alert: bool = False) -> None:
    safe_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text,
                                      "show_alert": alert})


def remember(db: Session, sender: dict) -> Courier:
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) \
        or str(sender["id"])
    username = (sender.get("username") or "").strip() or None
    courier = db.get(Courier, sender["id"])
    if courier is None:
        courier = Courier(telegram_id=sender["id"], name=name, username=username, active=False)
        db.add(courier)
    else:
        courier.name, courier.username = name, username or courier.username
    db.commit()
    return courier


def handle_message(db: Session, message: dict) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or not sender.get("id"):
        return                      # бот личный: в группах ему делать нечего
    courier = remember(db, sender)
    if not courier.active:
        send(chat["id"], PENDING)
        return
    text = (message.get("text") or "").strip()
    if text in (BTN_ORDERS, "/orders"):
        show_orders(db, chat["id"])
    elif text in (BTN_DELIVERY, "/delivery"):
        show_delivery(db, chat["id"])
    else:
        send(chat["id"], WELCOME, KEYBOARD)


def handle_callback(db: Session, background: BackgroundTasks, query: dict) -> None:
    callback_id = query.get("id")
    sender = query.get("from") or {}
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    action, _, raw_id = (query.get("data") or "").partition(":")
    courier = courier_of(db, sender.get("id") or 0)
    if courier is None:
        answer(callback_id, "Нет доступа. Попросите администратора включить вас в панели.", True)
        return
    try:
        order_id = int(raw_id)
    except ValueError:
        answer(callback_id)
        return

    def current() -> Order | None:
        db.expire_all()
        return db.get(Order, order_id)

    order = current()
    if order is None:
        answer(callback_id, "Заказа больше нет.", True)
        edit(chat_id, message_id, "Заказ удалён.", None)
        return

    if action == "take":
        if take(db, order_id, courier):
            order = current()
            answer(callback_id, "Заказ ваш")
            edit(chat_id, message_id, card(order) + "\n\n✅ <b>Вы приняли заказ.</b> "
                 f"Он в разделе «{BTN_DELIVERY}».", delivery_markup(order))
            staff(db, background, order, f"🚚 Заказ <b>{notify.esc(order.number)}</b> "
                                         f"взял доставщик {notify.esc(courier.name)}")
            log.info("Заказ %s взял доставщик %s", order.number, courier.telegram_id)
        else:
            order = current()
            if order.courier_id == courier.telegram_id and order.status == "DELIVERING":
                answer(callback_id, "Этот заказ уже ваш")
                edit(chat_id, message_id, card(order), delivery_markup(order))
                return
            answer(callback_id, "Не получилось: заказ уже взял другой доставщик."
                   if order.courier_id else "Заказ уже недоступен.", True)
            edit(chat_id, message_id, card(order) + "\n\n⛔ Заказ уже недоступен.", None)
        return

    if action in ("done", "drop"):
        if order.courier_id != courier.telegram_id or order.status != "DELIVERING":
            answer(callback_id, "Этот заказ уже не у вас.", True)
            edit(chat_id, message_id, card(order) + "\n\n⛔ Заказ уже не у вас.", None)
            return
        question = ("Покупатель получил заказ?" if action == "done"
                    else "Отказаться от заказа? Он снова станет свободным для всех.")
        answer(callback_id)
        edit(chat_id, message_id, card(order) + f"\n\n❓ <b>{question}</b>",
             confirm_markup(order, action))
        return

    if action == "back":
        answer(callback_id)
        mine_now = order.courier_id == courier.telegram_id and order.status == "DELIVERING"
        edit(chat_id, message_id, card(order), delivery_markup(order) if mine_now else None)
        return

    if action == "done!":
        if finish(db, order_id, courier):
            order = current()
            answer(callback_id, "Заказ выполнен")
            edit(chat_id, message_id, card(order) + "\n\n✅ <b>Доставлен.</b>", None)
            cash = "" if order.paid_at else f" Получено наличными: {notify.money(order.total)}."
            staff(db, background, order, f"✅ Заказ <b>{notify.esc(order.number)}</b> доставлен — "
                                         f"{notify.esc(courier.name)}.{cash}")
            log.info("Заказ %s доставлен, доставщик %s", order.number, courier.telegram_id)
        else:
            answer(callback_id, "Заказ уже не у вас — его отменили или изменили.", True)
            edit(chat_id, message_id, card(current()) + "\n\n⛔ Заказ уже не у вас.", None)
        return

    if action == "drop!":
        if release(db, order_id, courier.telegram_id):
            order = current()
            answer(callback_id, "Вы отказались от заказа")
            edit(chat_id, message_id, card(order) + "\n\n↩️ Вы отказались от заказа. "
                 "Он снова свободен для всех.", None)
            staff(db, background, order, f"↩️ Доставщик {notify.esc(courier.name)} отказался "
                                         f"от заказа <b>{notify.esc(order.number)}</b> — заказ снова свободен")
            log.info("Доставщик %s отказался от заказа %s", courier.telegram_id, order.number)
        else:
            answer(callback_id, "Заказ уже не у вас.", True)
            edit(chat_id, message_id, card(current()) + "\n\n⛔ Заказ уже не у вас.", None)
        return

    answer(callback_id)


def process(db: Session, background: BackgroundTasks, update_: dict) -> None:
    if "callback_query" in update_:
        handle_callback(db, background, update_["callback_query"])
    elif "message" in update_:
        handle_message(db, update_["message"])


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def courier_webhook(
    request: Request,
    background: BackgroundTasks,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
):
    """Обновления бота доставщиков. «Ок» всегда: иначе Telegram повторяет
    обновление и в конце концов отключает вебхук."""
    if not SECRET or not hmac.compare_digest((secret or "").encode(), SECRET.encode()):
        log.warning("Обновление бота доставщиков с неверным секретом отклонено")
        return {"ok": False}
    try:
        update_ = await request.json()
    except ValueError:
        return {"ok": True}
    try:
        await run_in_threadpool(process, db, background, update_)
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Бот доставщиков: обновление не разобрано")
    return {"ok": True}
