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

Язык — по настройке Telegram у доставщика: узбекский или русский. Запоминаем
его у доставщика, потому что часть сообщений уходит не в ответ на его
действие — «заказ отменён» из панели или с кассы.

Вебхук — /tg/courier, свой токен и свой секрет:
`python -m scripts.setup_courier_bot`.
"""
import hmac
import logging
import os
import threading
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

# какие заказы можно взять: принятые магазином и ещё без доставщика
AVAILABLE = ("CONFIRMED", "ASSEMBLING", "DELIVERING")
# сколько заказов показывать за раз: каждый — отдельное сообщение
LIST_LIMIT = 15
ITEMS_SHOWN = 10

# ------------------------------------------------------------------ тексты
# Узбекский — латиницей, как в узбекском Telegram. Адреса, имена и названия
# товаров приходят из заказа и не переводятся.

LANGS = ("ru", "uz")
TEXTS = {
    "ru": {
        "btn_orders": "📦 Заказы",
        "btn_delivery": "🚚 Доставка",
        "welcome": "Это бот доставщиков ROZMART.\n\n"
                   "«{orders}» — свободные заказы, их можно принять.\n"
                   "«{delivery}» — ваши заказы в пути.",
        "pending": "Это бот доставщиков ROZMART.\n\n"
                   "Я вас запомнил. Чтобы видеть заказы, попросите администратора "
                   "включить вас в панели: «Состав каталога» → «Доставщики».",
        "granted": "Доступ к заказам включён.",
        "sum": "сум",
        "paid": "💳 <b>ОПЛАЧЕНО КАРТОЙ — деньги не брать</b>",
        "collect": "💵 Получить с покупателя: <b>{sum}</b>",
        "more": "…и ещё {n}",
        "take": "✅ Принять заказ",
        "done": "✔️ Завершить",
        "drop": "↩️ Отменить",
        "map": "📍 Открыть на карте",
        "yes_done": "Да, заказ доставлен",
        "yes_drop": "Да, отказаться от заказа",
        "back": "Назад",
        "none_free": "Свободных заказов нет.",
        "free": "Свободные заказы: {n}",
        "none_mine": "У вас нет заказов в доставке. Взять — в «{orders}».",
        "mine": "Ваши заказы в доставке: {n}",
        "no_access": "Нет доступа. Попросите администратора включить вас в панели.",
        "gone": "Заказа больше нет.",
        "deleted": "Заказ удалён.",
        "taken": "✅ Заказ {number} принят — он в разделе «{delivery}»",
        "taken_note": "✅ <b>Вы приняли заказ.</b> Он в разделе «{delivery}».",
        "already_yours": "Этот заказ уже ваш",
        "taken_by_other": "Не получилось: заказ уже взял другой доставщик.",
        "unavailable": "Заказ уже недоступен.",
        "unavailable_note": "⛔ Заказ уже недоступен.",
        "not_yours": "Этот заказ уже не у вас.",
        "not_yours_note": "⛔ Заказ уже не у вас.",
        "not_yours_changed": "Заказ уже не у вас — его отменили или изменили.",
        "ask_done": "Покупатель получил заказ?",
        "ask_drop": "Отказаться от заказа? Он снова станет свободным для всех.",
        "done_ok": "Заказ выполнен",
        "done_note": "✅ <b>Доставлен.</b>",
        "dropped": "Вы отказались от заказа",
        "dropped_note": "↩️ Вы отказались от заказа. Он снова свободен для всех.",
        "canceled": "❌ Заказ <b>{number}</b> отменён — не везите.\n{customer} · {address}",
    },
    "uz": {
        "btn_orders": "📦 Buyurtmalar",
        "btn_delivery": "🚚 Yetkazish",
        "welcome": "Bu ROZMART yetkazib beruvchilar boti.\n\n"
                   "«{orders}» — bo'sh buyurtmalar, ularni qabul qilish mumkin.\n"
                   "«{delivery}» — yo'ldagi buyurtmalaringiz.",
        "pending": "Bu ROZMART yetkazib beruvchilar boti.\n\n"
                   "Sizni eslab qoldim. Buyurtmalarni ko'rish uchun administratordan sizni "
                   "panelda yoqib qo'yishni so'rang: «Состав каталога» → «Доставщики».",
        "granted": "Buyurtmalarga ruxsat berildi.",
        "sum": "so'm",
        "paid": "💳 <b>KARTA ORQALI TO'LANGAN — pul olinmasin</b>",
        "collect": "💵 Xaridordan olinadi: <b>{sum}</b>",
        "more": "…yana {n} ta",
        "take": "✅ Buyurtmani qabul qilish",
        "done": "✔️ Yakunlash",
        "drop": "↩️ Bekor qilish",
        "map": "📍 Xaritada ochish",
        "yes_done": "Ha, buyurtma topshirildi",
        "yes_drop": "Ha, buyurtmadan voz kechaman",
        "back": "Orqaga",
        "none_free": "Bo'sh buyurtmalar yo'q.",
        "free": "Bo'sh buyurtmalar: {n}",
        "none_mine": "Yetkazishda buyurtmangiz yo'q. Olish uchun — «{orders}».",
        "mine": "Yetkazishdagi buyurtmalaringiz: {n}",
        "no_access": "Ruxsat yo'q. Administratordan sizni panelda yoqib qo'yishni so'rang.",
        "gone": "Bu buyurtma endi yo'q.",
        "deleted": "Buyurtma o'chirilgan.",
        "taken": "✅ {number} buyurtma qabul qilindi — u «{delivery}» bo'limida",
        "taken_note": "✅ <b>Siz buyurtmani qabul qildingiz.</b> U «{delivery}» bo'limida.",
        "already_yours": "Bu buyurtma allaqachon sizniki",
        "taken_by_other": "Bo'lmadi: buyurtmani boshqa yetkazib beruvchi olib bo'ldi.",
        "unavailable": "Buyurtma endi mavjud emas.",
        "unavailable_note": "⛔ Buyurtma endi mavjud emas.",
        "not_yours": "Bu buyurtma endi sizda emas.",
        "not_yours_note": "⛔ Buyurtma endi sizda emas.",
        "not_yours_changed": "Buyurtma endi sizda emas — uni bekor qilishgan yoki o'zgartirishgan.",
        "ask_done": "Xaridor buyurtmani oldimi?",
        "ask_drop": "Buyurtmadan voz kechasizmi? U yana hamma uchun bo'sh bo'ladi.",
        "done_ok": "Buyurtma bajarildi",
        "done_note": "✅ <b>Topshirildi.</b>",
        "dropped": "Siz buyurtmadan voz kechdingiz",
        "dropped_note": "↩️ Siz buyurtmadan voz kechdingiz. U yana hamma uchun bo'sh.",
        "canceled": "❌ <b>{number}</b> buyurtma bekor qilindi — olib bormang.\n"
                    "{customer} · {address}",
    },
}

# Кнопки внизу — обычный текст: что бы ни пришло, узнаём на любом языке.
# Человек мог сменить язык Telegram, а клавиатура у него осталась прежняя
ORDERS_BUTTONS = {TEXTS[lang]["btn_orders"] for lang in LANGS} | {"/orders"}
DELIVERY_BUTTONS = {TEXTS[lang]["btn_delivery"] for lang in LANGS} | {"/delivery"}


def t(lang: str, key: str, **values) -> str:
    texts = TEXTS.get(lang) or TEXTS["ru"]
    base = {"orders": texts["btn_orders"], "delivery": texts["btn_delivery"]}
    return texts[key].format(**{**base, **values})


def lang_of(sender: dict) -> str:
    """Язык по настройке Telegram: узбекский — у кого Telegram на узбекском."""
    return "uz" if (sender.get("language_code") or "").lower().startswith("uz") else "ru"


def keyboard(lang: str) -> dict:
    return {
        "keyboard": [[{"text": t(lang, "btn_orders")}, {"text": t(lang, "btn_delivery")}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def money(lang: str, value: int) -> str:
    return f"{value:,}".replace(",", " ") + " " + t(lang, "sum")


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


# Пока разбирается обновление, ответы не отправляются сразу, а копятся.
# Ответ на нажатие кнопки уходит прямо в ответе на вебхук — мгновенно и без
# отдельного запроса, а остальное (убрать сообщение, прислать список,
# сказать сотрудникам) — следом, в фоне. Иначе кнопка «крутилась» бы, пока
# сервер по очереди переговорит с Telegram обо всём.
_out = threading.local()


def _later(fn, *args) -> None:
    box = getattr(_out, "box", None)
    if box is None:
        fn(*args)                   # вне вебхука (панель, синхронизация) — сразу
    else:
        box.append((fn, args))


def flush(box: list) -> None:
    for fn, args in box:
        try:
            fn(*args)
        except Exception:           # noqa: BLE001
            log.exception("Бот доставщиков: ответ не отправлен")


def send(chat_id: int, text: str, markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if markup is not None:
        payload["reply_markup"] = markup
    _later(safe_call, "sendMessage", payload)


# ------------------------------------------------------------------ карточка

def map_url(order: Order) -> str:
    """Карта: по точке, если покупатель её поставил, иначе поиск по адресу."""
    if order.lat is not None and order.lon is not None:
        return f"https://maps.google.com/?q={order.lat},{order.lon}"
    return "https://maps.google.com/?q=" + quote(order.address)


def card(order: Order, lang: str) -> str:
    """Всё, что нужно доставщику: деньги, кому, куда, когда и что везти."""
    money_line = (t(lang, "paid") if order.paid_at is not None
                  else t(lang, "collect", sum=money(lang, order.total)))
    lines = [f"📦 <b>{notify.esc(order.number)}</b> — {money(lang, order.total)}", money_line, "",
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
        lines.append(t(lang, "more", n=len(order.items) - ITEMS_SHOWN))
    return "\n".join(lines)


def take_markup(order: Order, lang: str) -> dict:
    return {"inline_keyboard": [[{"text": t(lang, "take"), "callback_data": f"take:{order.id}"}]]}


def delivery_markup(order: Order, lang: str) -> dict:
    return {"inline_keyboard": [
        [{"text": t(lang, "done"), "callback_data": f"done:{order.id}"},
         {"text": t(lang, "drop"), "callback_data": f"drop:{order.id}"}],
        [{"text": t(lang, "map"), "url": map_url(order)}],
    ]}


def confirm_markup(order: Order, action: str, lang: str) -> dict:
    return {"inline_keyboard": [
        [{"text": t(lang, "yes_done" if action == "done" else "yes_drop"),
          "callback_data": f"{action}!:{order.id}"}],
        [{"text": t(lang, "back"), "callback_data": f"back:{order.id}"}],
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


def show_orders(db: Session, chat_id: int, lang: str) -> None:
    orders = available(db)
    if not orders:
        send(chat_id, t(lang, "none_free"), keyboard(lang))
        return
    send(chat_id, t(lang, "free", n=len(orders)), keyboard(lang))
    for order in orders:
        send(chat_id, card(order, lang), take_markup(order, lang))


def show_delivery(db: Session, chat_id: int, lang: str) -> None:
    orders = mine(db, chat_id)
    if not orders:
        send(chat_id, t(lang, "none_mine"), keyboard(lang))
        return
    send(chat_id, t(lang, "mine", n=len(orders)), keyboard(lang))
    for order in orders:
        send(chat_id, card(order, lang), delivery_markup(order, lang))


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
    """Сообщение сотрудникам в служебный бот: кто взял, кто доставил.
    Служебный бот общий для магазина — по-русски."""
    rows = notify.queue_staff(db, order, text)
    if not rows:
        return
    db.commit()
    ids = [row.id for row in rows]
    if getattr(_out, "box", None) is not None:
        _later(notify.send_many, ids)       # после ответа доставщику, не раньше
    elif background is not None:
        background.add_task(notify.send_many, ids)
    else:
        notify.send_many(ids)


def language(courier_id: int) -> str:
    """Язык доставщика — для сообщений не в ответ на его действие."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        courier = db.get(Courier, courier_id)
        return (courier.language if courier and courier.language in LANGS else "ru")
    finally:
        db.close()


def access_granted(courier: Courier) -> None:
    lang = courier.language if courier.language in LANGS else "ru"
    send(courier.telegram_id, t(lang, "granted") + "\n\n" + t(lang, "welcome"), keyboard(lang))


def order_canceled(order: Order) -> None:
    """Заказ отменили в панели или на кассе, а он у доставщика в пути —
    сказать ему, чтобы не вёз."""
    if not enabled() or not order.courier_id:
        return
    lang = language(order.courier_id)
    send(order.courier_id, t(lang, "canceled", number=notify.esc(order.number),
                             customer=notify.esc(order.customer_name),
                             address=notify.esc(order.address)))


# ------------------------------------------------------------------ обработка

def edit(chat_id: int, message_id: int, text: str, markup: dict | None) -> None:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup is not None:
        payload["reply_markup"] = markup
    _later(safe_call, "editMessageText", payload)


def remove(chat_id: int, message_id: int, fallback: str) -> None:
    """Убирает сообщение с заказом из чата."""
    _later(_remove_now, chat_id, message_id, fallback)


def _remove_now(chat_id: int, message_id: int, fallback: str) -> None:
    """Telegram даёт удалять свои сообщения только 48 часов — старше
    не удалится, тогда хотя бы снимаем с него кнопки и пишем, что с ним стало."""
    try:
        call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except notify.NotifyError as exc:
        log.info("Бот доставщиков: сообщение %s не удалено (%s)", message_id, exc)
        edit(chat_id, message_id, fallback, None)


def answer(callback_id: str, text: str = "", alert: bool = False) -> None:
    payload = {"callback_query_id": callback_id, "text": text, "show_alert": alert}
    if getattr(_out, "box", None) is not None and getattr(_out, "inline", None) is None:
        _out.inline = {"method": "answerCallbackQuery", **payload}   # в ответе на вебхук
    else:
        _later(safe_call, "answerCallbackQuery", payload)


def remember(db: Session, sender: dict) -> Courier:
    """Запоминает написавшего — без доступа — и его язык."""
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) \
        or str(sender["id"])
    username = (sender.get("username") or "").strip() or None
    courier = db.get(Courier, sender["id"])
    if courier is None:
        courier = Courier(telegram_id=sender["id"], name=name, username=username, active=False)
        db.add(courier)
    else:
        courier.name, courier.username = name, username or courier.username
    courier.language = lang_of(sender)
    db.commit()
    return courier


def handle_message(db: Session, message: dict) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or not sender.get("id"):
        return                      # бот личный: в группах ему делать нечего
    courier = remember(db, sender)
    lang = courier.language
    if not courier.active:
        send(chat["id"], t(lang, "pending"))
        return
    text = (message.get("text") or "").strip()
    if text in ORDERS_BUTTONS:
        show_orders(db, chat["id"], lang)
    elif text in DELIVERY_BUTTONS:
        show_delivery(db, chat["id"], lang)
    else:
        send(chat["id"], t(lang, "welcome"), keyboard(lang))


def handle_callback(db: Session, background: BackgroundTasks, query: dict) -> None:
    callback_id = query.get("id")
    sender = query.get("from") or {}
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    action, _, raw_id = (query.get("data") or "").partition(":")
    lang = lang_of(sender)
    courier = courier_of(db, sender.get("id") or 0)
    if courier is None:
        answer(callback_id, t(lang, "no_access"), True)
        return
    if courier.language != lang:
        courier.language = lang         # сменил язык Telegram — следуем за ним
        db.commit()
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
        answer(callback_id, t(lang, "gone"), True)
        edit(chat_id, message_id, t(lang, "deleted"), None)
        return

    if action == "take":
        if take(db, order_id, courier):
            order = current()
            # Принятый заказ из списка свободных убираем: в чате остаются
            # только те, что ещё можно взять. Он теперь в «Доставке»
            answer(callback_id, t(lang, "taken", number=order.number), True)
            remove(chat_id, message_id, card(order, lang) + "\n\n" + t(lang, "taken_note"))
            staff(db, background, order, f"🚚 Заказ <b>{notify.esc(order.number)}</b> "
                                         f"взял доставщик {notify.esc(courier.name)}")
            log.info("Заказ %s взял доставщик %s", order.number, courier.telegram_id)
        else:
            order = current()
            if order.courier_id == courier.telegram_id and order.status == "DELIVERING":
                answer(callback_id, t(lang, "already_yours"))
                edit(chat_id, message_id, card(order, lang), delivery_markup(order, lang))
                return
            answer(callback_id, t(lang, "taken_by_other" if order.courier_id else "unavailable"),
                   True)
            remove(chat_id, message_id, card(order, lang) + "\n\n" + t(lang, "unavailable_note"))
        return

    if action in ("done", "drop"):
        if order.courier_id != courier.telegram_id or order.status != "DELIVERING":
            answer(callback_id, t(lang, "not_yours"), True)
            edit(chat_id, message_id, card(order, lang) + "\n\n" + t(lang, "not_yours_note"), None)
            return
        answer(callback_id)
        question = t(lang, "ask_done" if action == "done" else "ask_drop")
        edit(chat_id, message_id, card(order, lang) + f"\n\n❓ <b>{question}</b>",
             confirm_markup(order, action, lang))
        return

    if action == "back":
        answer(callback_id)
        mine_now = order.courier_id == courier.telegram_id and order.status == "DELIVERING"
        edit(chat_id, message_id, card(order, lang),
             delivery_markup(order, lang) if mine_now else None)
        return

    if action == "done!":
        if finish(db, order_id, courier):
            order = current()
            answer(callback_id, t(lang, "done_ok"))
            edit(chat_id, message_id, card(order, lang) + "\n\n" + t(lang, "done_note"), None)
            cash = "" if order.paid_at else f" Получено наличными: {notify.money(order.total)}."
            staff(db, background, order, f"✅ Заказ <b>{notify.esc(order.number)}</b> доставлен — "
                                         f"{notify.esc(courier.name)}.{cash}")
            log.info("Заказ %s доставлен, доставщик %s", order.number, courier.telegram_id)
        else:
            answer(callback_id, t(lang, "not_yours_changed"), True)
            edit(chat_id, message_id,
                 card(current(), lang) + "\n\n" + t(lang, "not_yours_note"), None)
        return

    if action == "drop!":
        if release(db, order_id, courier.telegram_id):
            order = current()
            answer(callback_id, t(lang, "dropped"))
            edit(chat_id, message_id, card(order, lang) + "\n\n" + t(lang, "dropped_note"), None)
            staff(db, background, order,
                  f"↩️ Доставщик {notify.esc(courier.name)} отказался от заказа "
                  f"<b>{notify.esc(order.number)}</b> — заказ снова свободен")
            log.info("Доставщик %s отказался от заказа %s", courier.telegram_id, order.number)
        else:
            answer(callback_id, t(lang, "not_yours"), True)
            edit(chat_id, message_id,
                 card(current(), lang) + "\n\n" + t(lang, "not_yours_note"), None)
        return

    answer(callback_id)


def process(db: Session, background: BackgroundTasks, update_: dict):
    """Разбор обновления. Возвращает ответ для тела вебхука (или None)
    и то, что отправить следом."""
    _out.box, _out.inline = [], None
    try:
        if "callback_query" in update_:
            handle_callback(db, background, update_["callback_query"])
        elif "message" in update_:
            handle_message(db, update_["message"])
        return _out.inline, _out.box
    finally:
        _out.box, _out.inline = None, None


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
        inline, box = await run_in_threadpool(process, db, background, update_)
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Бот доставщиков: обновление не разобрано")
        return {"ok": True}
    if box:
        background.add_task(flush, box)
    return inline or {"ok": True}
