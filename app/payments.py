"""Оплата картой через Telegram Payments.

Покупатель платит в окне Telegram, не выходя из приложения; провайдер — Click.
Путь заказа:

1. Оформление: заказ получает NEW «Ждёт оплаты» и в REGOS не уходит — NEW нет
   в PUSHABLE (app/regos/orders_push.py). Сотрудникам тоже ничего не приходит:
   неоплаченный заказ собирать не надо.
2. Приложение просит счёт (`create_invoice_link`) и открывает его
   `Telegram.WebApp.openInvoice`.
3. Перед списанием Telegram спрашивает сервер, можно ли (`pre_checkout_query`),
   и ждёт ответа не дольше 10 секунд. Соглашаемся, только если заказ есть,
   всё ещё ждёт оплаты и сумма та же до сума.
4. После списания приходит `successful_payment`. Только оно — правда о деньгах:
   статус «paid» в приложении говорит лишь, что окно закрылось. Заказ переходит
   в CONFIRMED и дальше идёт тем же путём, что наличный: в REGOS, сотрудникам.

Повторная доставка уведомления об оплате — обычное дело у Telegram. Второй раз
провести платёж не даёт уникальный `telegram_payment_charge_id` в базе.

Тестовый режим. Токен провайдера вида `…:TEST:…` денег не списывает. Витрина
при этом боевая, с настоящими покупателями, и если показать им оплату картой,
любой «оплатит» заказ тестовой картой, а кассир отдаст товар по пометке
«оплачено». Поэтому, пока токен тестовый, оплату картой видят и могут выбрать
только тестировщики из PAYMENT_TEST_USERS. Проверяет это сервер, а не витрина.
"""
import logging
import os
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from app import notify
from app.models import Order, utcnow
from app.telegram import BOT_TOKEN

log = logging.getLogger(__name__)

# Токен провайдера из BotFather. В репозитории его нет — только на сервере.
TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()

# Кто может платить картой, пока токен тестовый. Telegram-id через запятую.
TEST_USERS = {
    int(x) for x in os.getenv("PAYMENT_TEST_USERS", "").replace(" ", "").split(",") if x
}

# Через сколько минут неоплаченный заказ отменяется.
UNPAID_MINUTES = int(os.getenv("UNPAID_ORDER_MINUTES", "30"))

# Пока окно оплаты открыто, заказ по таймауту не отменяем. Пять минут — с запасом
# на ввод карты и подтверждение по СМС.
CHECKOUT_GRACE = timedelta(minutes=5)

CURRENCY = "UZS"
# У сума в Telegram два знака после запятой (exp=2): суммы передаются в тийинах.
# 59 990 сумов — это 5 999 000.
MINOR = 100

# Как часто ищем просроченные неоплаченные заказы.
SWEEP_SECONDS = 60

# Что увидит покупатель в окне оплаты, если платить нельзя. Текст — ему, а не нам.
REFUSE = {
    "missing": "Заказ не найден. Оформите его заново.",
    "canceled": "Заказ отменён — оплатить его уже нельзя.",
    "paid": "Этот заказ уже оплачен.",
    "not_card": "Этот заказ оформлен с оплатой наличными.",
    "amount": "Сумма заказа изменилась. Откройте заказ в приложении и оплатите заново.",
}


class PaymentError(Exception):
    """Счёт выставить не удалось. Текст — для покупателя."""


def enabled() -> bool:
    return bool(TOKEN)


def is_test() -> bool:
    return ":TEST:" in TOKEN


def available_for(telegram_id: int | None) -> bool:
    """Можно ли этому человеку платить картой."""
    if not enabled():
        return False
    if is_test():
        return telegram_id is not None and telegram_id in TEST_USERS
    return True


def awaiting_payment(order: Order) -> bool:
    """Заказ ждёт оплаты картой: оформлен онлайн, денег ещё нет."""
    return order.payment_method == "online" and order.paid_at is None \
        and order.status == "NEW"


def pay_until(order: Order):
    """До какого времени неоплаченный заказ живёт."""
    if not awaiting_payment(order):
        return None
    return order.created_at + timedelta(minutes=UNPAID_MINUTES)


def prices(order: Order) -> list[dict]:
    """Строки счёта: товары и отдельно доставка.

    Покупатель должен видеть, за что платит. Сумма строк обязана сойтись
    с заказом до тийина — её же Telegram пришлёт в pre_checkout_query,
    и там мы её сверяем.
    """
    rows = []
    for item in order.items:
        count = f" × {item.quantity}" if item.quantity > 1 else ""
        rows.append({
            "label": f"{item.product_name} {item.weight}{count}"[:60],
            "amount": item.price * item.quantity * MINOR,
        })
    rows.append({"label": "Доставка", "amount": order.delivery_price * MINOR})
    return rows


def create_invoice_link(order: Order) -> str:
    """Ссылка на счёт для Telegram.WebApp.openInvoice.

    Выставляет магазинный бот — тот, из которого открыто приложение:
    платёж Telegram привязывает к боту, чей счёт.
    """
    names = ", ".join(i.product_name for i in order.items[:3])
    if len(order.items) > 3:
        names += f" и ещё {len(order.items) - 3}"
    try:
        return notify.call("createInvoiceLink", {
            "title": f"Заказ {order.number}",
            "description": f"{names}. Доставка: {order.delivery_slot}"[:255],
            # по нему находим заказ, когда Telegram спрашивает и когда сообщает
            "payload": order.number,
            "provider_token": TOKEN,
            "currency": CURRENCY,
            "prices": prices(order),
        }, token=BOT_TOKEN)
    except notify.NotifyError as exc:
        log.warning("Счёт по заказу %s не выставлен: %s", order.number, exc)
        if "TOTAL_AMOUNT" in str(exc).upper():
            raise PaymentError(
                "Сумма меньше минимальной для оплаты картой. Выберите оплату наличными."
            ) from exc
        raise PaymentError("Не удалось выставить счёт. Попробуйте ещё раз.") from exc


def find(db, number: str) -> Order | None:
    return db.scalar(select(Order).where(Order.number == number))


def check_pre_checkout(db, query: dict) -> tuple[bool, str | None]:
    """Можно ли списывать деньги. Возвращает (да/нет, текст отказа)."""
    order = find(db, query.get("invoice_payload") or "")
    if order is None:
        return False, REFUSE["missing"]
    if order.payment_method != "online":
        return False, REFUSE["not_card"]
    if order.paid_at is not None:
        return False, REFUSE["paid"]
    if order.status != "NEW":
        return False, REFUSE["canceled"]
    if query.get("currency") != CURRENCY or query.get("total_amount") != order.total * MINOR:
        log.warning("Заказ %s: сумма в окне оплаты %s %s, в заказе %s",
                    order.number, query.get("total_amount"), query.get("currency"),
                    order.total * MINOR)
        return False, REFUSE["amount"]
    # окно оплаты открыто: пока человек вводит карту, заказ не отменяем
    order.checkout_at = utcnow()
    db.commit()
    return True, None


def answer_pre_checkout(query_id: str, ok: bool, error: str | None) -> None:
    payload = {"pre_checkout_query_id": query_id, "ok": ok}
    if not ok:
        payload["error_message"] = error
    notify.call("answerPreCheckoutQuery", payload, token=BOT_TOKEN)


def record_payment(db, payment: dict) -> tuple[str, Order | None]:
    """Проводит платёж. Возвращает, что вышло, и заказ.

    'confirmed' — заказ оплачен и принят, дальше в REGOS и сотрудникам;
    'duplicate' — это уведомление уже проводили, ничего не делаем;
    'late'      — деньги пришли за заказ, который уже не ждал оплаты
                  (отменён или оплачен другим платежом) — нужен человек;
    'unknown'   — заказа с таким номером нет, а деньги списаны.
    """
    charge = payment.get("telegram_payment_charge_id")
    if charge and db.scalar(select(Order).where(Order.payment_charge_id == charge)):
        return "duplicate", None

    order = find(db, payment.get("invoice_payload") or "")
    if order is None:
        log.error("Оплата %s по неизвестному заказу %s", charge, payment.get("invoice_payload"))
        return "unknown", None

    if order.paid_at is not None or order.status != "NEW":
        # Второй платёж по оплаченному или оплата отменённого: деньги списаны,
        # а заказ их уже не ждал. Молча проводить нельзя ни то, ни другое.
        log.error("Заказ %s: оплата %s пришла в состоянии %s, оплачен ранее: %s",
                  order.number, charge, order.status, order.paid_at)
        return "late", order

    order.paid_at = utcnow()
    order.payment_charge_id = charge
    order.provider_charge_id = payment.get("provider_payment_charge_id")
    order.paid_amount = (payment.get("total_amount") or 0) // MINOR
    order.status = "CONFIRMED"
    db.commit()
    log.info("Заказ %s оплачен картой, платёж %s", order.number, charge)
    return "confirmed", order


def cancel_stale(db) -> list[str]:
    """Отменяет неоплаченные заказы старше UNPAID_MINUTES.

    В REGOS такие не уходили (NEW не выгружается), поэтому отмена только у нас.
    Заказ с открытым окном оплаты не трогаем: деньги могут вот-вот списаться.
    """
    now = utcnow()
    stale = db.scalars(select(Order).where(
        Order.payment_method == "online",
        Order.status == "NEW",
        Order.paid_at.is_(None),
        Order.created_at < now - timedelta(minutes=UNPAID_MINUTES),
    )).all()
    canceled = []
    for order in stale:
        if order.checkout_at and order.checkout_at > now - CHECKOUT_GRACE:
            continue
        order.status = "CANCELED"
        canceled.append(order.number)
    if canceled:
        db.commit()
        log.info("Отменены неоплаченные заказы: %s", ", ".join(canceled))
    return canceled


def worker() -> None:
    from app.db import SessionLocal

    while True:
        db = SessionLocal()
        try:
            cancel_stale(db)
        except Exception:                       # noqa: BLE001
            # поток обязан пережить что угодно: иначе неоплаченные заказы
            # начнут копиться, а служба останется живой, и никто не заметит
            db.rollback()
            log.exception("Сбой отмены неоплаченных заказов")
        finally:
            db.close()
        time.sleep(SWEEP_SECONDS)


_started = False


def start_worker() -> None:
    global _started
    if _started or not enabled():
        return
    _started = True
    threading.Thread(target=worker, name="payments", daemon=True).start()
    log.info("Оплата картой включена%s", " (тестовый режим)" if is_test() else "")
