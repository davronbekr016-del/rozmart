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
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import notify, payments
from app.db import get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["Магазинный бот"])

SECRET = os.getenv("SHOP_WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/tg/shop"


def handle_pre_checkout(db: Session, query: dict) -> None:
    """Отвечает, можно ли списывать. Молчать нельзя: без ответа за 10 секунд
    Telegram отменит платёж, а покупатель увидит невнятную ошибку."""
    try:
        ok, error = payments.check_pre_checkout(db, query)
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Не проверили заказ перед оплатой")
        ok, error = False, "Не получилось проверить заказ. Попробуйте ещё раз через минуту."
    try:
        payments.answer_pre_checkout(query["id"], ok, error)
    except notify.NotifyError as exc:
        log.warning("Не ответили на запрос перед оплатой: %s", exc)


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


def handle_payment(db: Session, background: BackgroundTasks, payment: dict) -> None:
    """Проводит платёж и запускает заказ дальше тем же путём, что наличный."""
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

    if outcome == "duplicate":
        return
    if outcome == "confirmed":
        # сотрудникам — только теперь, с пометкой «оплачено»: до оплаты
        # неоплаченный заказ им собирать было незачем
        rows = notify.queue_staff(db, order, notify.staff_new_order(order))
        if rows:
            db.commit()
            background.add_task(notify.send_many, [row.id for row in rows])
        from app.regos.orders_push import push_one
        background.add_task(push_one, order.id)
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


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def shop_webhook(
    request: Request,
    background: BackgroundTasks,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
):
    """Обновления магазинного бота.

    «ок» отвечаем всегда: на любой другой ответ Telegram повторяет обновление,
    а повтор уведомления об оплате мы и так переживаем — но зачем его провоцировать.
    """
    if not SECRET or secret != SECRET:
        log.warning("Обновление магазинного бота с неверным секретом отклонено")
        return {"ok": False}
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}

    try:
        if "pre_checkout_query" in update:
            handle_pre_checkout(db, update["pre_checkout_query"])
        else:
            payment = (update.get("message") or {}).get("successful_payment")
            if payment:
                handle_payment(db, background, payment)
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Не разобрали обновление магазинного бота")
    return {"ok": True}
