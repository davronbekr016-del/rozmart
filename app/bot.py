"""Приём сообщений служебного бота.

Бот не покупательский: он рассказывает магазину о заказах, а покупатель следит
за своим заказом в приложении. Поэтому человеку, который написал боту просто
так, он отвечает, что чат служебный, и ничего больше не делает.

Принять «Старт» бот всё равно обязан: Telegram не даёт боту написать первым,
и пока чат ему не написал, отправить туда нельзя ничего (см. app/notify.py).
Написавший чат запоминается — чтобы администратор увидел его в панели
и выдал доступ к заказам. Сам себе доступ не выдаёт никто: в карточке заказа
телефон и адрес покупателя.

Telegram шлёт обновления сюда вебхуком. Адрес регистрируется командой
`python -m scripts.setup_bot`; она же задаёт секрет, который Telegram
возвращает в заголовке каждого запроса. Без секрета обработчик не работает:
открытый адрес позволил бы кому угодно подписать на бота чужой чат и слать
через нашего бота сообщения незнакомым людям.
"""
import logging
import os

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app import notify
from app.db import get_db
from app.models import StaffChat
from app.telegram import TelegramUser, current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["Служебный бот"])

SECRET = os.getenv("NOTIFY_WEBHOOK_SECRET", "").strip()

# Адрес намеренно не угадывается по имени: даже с проверкой секрета лишний
# известный адрес — лишние запросы, которые придётся разбирать.
WEBHOOK_PATH = "/tg/hook"

WELCOME_STAFF = (
    "Этот чат подключён к заказам ROZMART.\n\n"
    "Сюда приходит каждый новый заказ и сообщения об отменах."
)

WELCOME_PENDING = (
    "Это служебный бот магазина ROZMART — сюда приходят заказы сотрудникам.\n\n"
    "Чат я запомнил. Чтобы заказы приходили и сюда, попросите администратора "
    "включить его в панели: «Состав каталога» → «Кому приходят заказы».\n\n"
    "Если вы покупатель — заказ и его состояние видно в самом приложении "
    "магазина, в разделе «Мои заказы»."
)


def remember_chat(db: Session, chat_id: int, title: str, kind: str,
                  username: str | None = None) -> StaffChat:
    """Запоминает чат, чтобы администратор увидел его в панели.

    Доступ к заказам чат так не получает: active остаётся выключенным, пока
    его не включат руками. Смысл записи один — чтобы администратору было
    что включать, не выясняя чей-то chat_id.

    Название и @имя обновляем при каждом обращении: человек их меняет когда
    захочет, а в списке нужно то, по которому его узнают сегодня.
    """
    chat = db.get(StaffChat, chat_id)
    if chat is None:
        chat = StaffChat(chat_id=chat_id, title=title, kind=kind,
                         username=username, active=False)
        db.add(chat)
        return chat
    if title:
        chat.title = title
    if username:
        chat.username = username
    return chat


def has_access(db: Session, chat_id: int) -> bool:
    chat = db.get(StaffChat, chat_id)
    return bool(chat and chat.active)


def reply(chat_id: int, text: str) -> None:
    """Ответ на сообщение человека. В очередь не кладём: это переписка,
    а не уведомление — не дошло сейчас, повторять через минуту незачем."""
    try:
        notify.send_message(chat_id, text)
    except notify.NotifyError as exc:
        log.warning("Не ответили в чат %s: %s", chat_id, exc)


def handle_message(db: Session, message: dict) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    kind = chat.get("type")
    if not chat_id or kind not in {"private", "group", "supergroup"}:
        return                       # каналы и прочая экзотика нас не касаются

    if kind == "private":
        title = " ".join(filter(None, [sender.get("first_name"),
                                       sender.get("last_name")])) or str(chat_id)
        username = (sender.get("username") or "").strip() or None
        remember_chat(db, chat_id, title, "private", username)
    else:
        remember_chat(db, chat_id, chat.get("title") or "группа", "group")
    db.commit()

    # В группе на каждое сообщение не отвечаем: бот там сидит молча и ждёт,
    # пока его включат. Отвечаем только на прямое обращение.
    text = (message.get("text") or "").strip()
    if kind != "private" and not text.startswith(("/start", "/staff")):
        return
    reply(chat_id, WELCOME_STAFF if has_access(db, chat_id) else WELCOME_PENDING)


def handle_membership(db: Session, update: dict) -> None:
    """Бота добавили в группу, удалили оттуда или заблокировали в личном чате.

    Без этого обновления о блокировке мы узнавали бы только при отправке —
    то есть каждый раз ошибкой.
    """
    chat = update.get("chat") or {}
    sender = update.get("from") or {}
    status = ((update.get("new_chat_member") or {}).get("status") or "")
    chat_id = chat.get("id")
    if not chat_id:
        return

    if status in {"kicked", "left"}:
        known = db.get(StaffChat, chat_id)
        if known is not None:
            known.active = False     # выгнали — слать туда больше некуда
        db.commit()
        return

    if chat.get("type") in {"group", "supergroup"}:
        remember_chat(db, chat_id, chat.get("title") or "группа", "group")
    else:
        title = " ".join(filter(None, [sender.get("first_name"),
                                       sender.get("last_name")])) or str(chat_id)
        remember_chat(db, chat_id, title, "private",
                      (sender.get("username") or "").strip() or None)
    db.commit()
    if not has_access(db, chat_id):
        reply(chat_id, WELCOME_PENDING)


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def webhook(
    request: Request,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
):
    """Обновления от Telegram.

    Отвечаем «ок» всегда, даже на то, что не поняли: на любой другой ответ
    Telegram повторяет обновление и в конце концов отключает вебхук — из-за
    одного неожиданного сообщения перестали бы работать все уведомления.
    """
    if not SECRET or secret != SECRET:
        log.warning("Обновление с неверным секретом отклонено")
        return {"ok": False}

    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}

    try:
        if "message" in update:
            handle_message(db, update["message"])
        elif "my_chat_member" in update:
            handle_membership(db, update["my_chat_member"])
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Не разобрали обновление Telegram")
    return {"ok": True}
