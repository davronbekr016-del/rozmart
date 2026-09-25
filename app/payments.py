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

from sqlalchemy import or_, select

from app import notify, order_status
from app.models import Order, utcnow
from app.telegram import BOT_TOKEN

log = logging.getLogger(__name__)

# Токен провайдера из BotFather. В репозитории его нет — только на сервере.
TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()

# Секрет вебхука магазинного бота. Без него вебхук отвергает всё, и оплата
# гарантированно не пройдёт: запрос перед списанием уйдёт в тайм-аут.
# Поэтому без секрета оплата картой считается выключенной.
SHOP_SECRET = os.getenv("SHOP_WEBHOOK_SECRET", "").strip()

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
    "busy": "Заказ уже в работе у магазина — оплатить его здесь нельзя. Свяжитесь с магазином.",
    "paid": "Этот заказ уже оплачен.",
    "not_card": "Этот заказ оформлен с оплатой наличными.",
    "amount": "Сумма заказа изменилась. Откройте заказ в приложении и оплатите заново.",
}


class PaymentError(Exception):
    """Счёт выставить не удалось. Текст — для покупателя."""


def enabled() -> bool:
    return bool(TOKEN and SHOP_SECRET)


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


def checkout_open(order: Order) -> bool:
    """Покупатель сейчас в окне оплаты: Telegram недавно спрашивал «можно ли»."""
    return (awaiting_payment(order) and order.checkout_at is not None
            and order.checkout_at > utcnow() - CHECKOUT_GRACE)


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


# Выставленные счета: номер заказа -> (сумма, ссылка, когда). Ссылка на счёт
# живёт, пока по ней не заплатили, и выставлять новую на каждое нажатие
# «Оплатить» незачем. А главное — нельзя: каждый createInvoiceLink идёт в лимит
# запросов магазинного бота, и упёршийся в него бот перестанет отвечать
# на запросы перед списанием у всех покупателей сразу.
_invoices: dict[str, tuple[int, str, float]] = {}
INVOICE_TTL = 30 * 60


def create_invoice_link(order: Order) -> str:
    """Ссылка на счёт для Telegram.WebApp.openInvoice.

    Выставляет магазинный бот — тот, из которого открыто приложение:
    платёж Telegram привязывает к боту, чей счёт.
    """
    cached = _invoices.get(order.number)
    if cached and cached[0] == order.total and time.time() - cached[2] < INVOICE_TTL:
        return cached[1]
    link = _create_invoice_link(order)
    _invoices[order.number] = (order.total, link, time.time())
    return link


def _create_invoice_link(order: Order) -> str:
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
    if order.status == "CANCELED":
        return False, REFUSE["canceled"]
    if order.status != "NEW":
        return False, REFUSE["busy"]
    if query.get("currency") != CURRENCY or query.get("total_amount") != order.total * MINOR:
        log.warning("Заказ %s: сумма в окне оплаты %s %s, в заказе %s",
                    order.number, query.get("total_amount"), query.get("currency"),
                    order.total * MINOR)
        return False, REFUSE["amount"]
    # окно оплаты открыто: пока человек вводит карту, заказ не отменяем
    order.checkout_at = utcnow()
    db.commit()
    return True, None


def record_payment(db, payment: dict) -> tuple[str, Order | None]:
    """Проводит платёж. Возвращает, что вышло, и заказ.

    'confirmed' — заказ оплачен и принят, дальше в REGOS и сотрудникам;
    'duplicate' — это уведомление уже проводили, ничего не делаем;
    'late'      — деньги пришли за заказ, который уже не ждал оплаты
                  (отменён или оплачен другим платежом) — нужен человек;
    'unknown'   — заказа с таким номером нет, а деньги списаны.
    """
    charge = payment.get("telegram_payment_charge_id")
    if charge:
        already = db.scalar(select(Order).where(Order.payment_charge_id == charge))
        if already is not None:
            return "duplicate", already

    order = find(db, payment.get("invoice_payload") or "")
    if order is None:
        log.error("Оплата %s по неизвестному заказу %s", charge, payment.get("invoice_payload"))
        return "unknown", None

    if (payment.get("currency") != CURRENCY
            or payment.get("total_amount") != order.total * MINOR):
        # Сумму сверяли перед списанием, и счёт выставляем сами — расхождения
        # быть не должно. Но если оно есть, заказ по такому платежу не принимаем:
        # пусть разберётся человек
        log.error("Заказ %s: оплачено %s %s, а заказ на %s", order.number,
                  payment.get("total_amount"), payment.get("currency"), order.total * MINOR)
        return "late", order

    if order.paid_at is not None or order.status != "NEW":
        # Второй платёж по оплаченному или оплата отменённого: деньги списаны,
        # а заказ их уже не ждал. Молча проводить нельзя ни то, ни другое.
        log.error("Заказ %s: оплата %s пришла в состоянии %s, оплачен ранее: %s",
                  order.number, charge, order.status, order.paid_at)
        return "late", order

    # Условная запись: заказ всё ещё NEW и не оплачен. Если в эту же долю
    # секунды его отменила автоотмена или провёл второй платёж, запись
    # не пройдёт, и деньги уйдут по ветке «нужен человек», а не пропадут
    moved = order_status.move(db, order, "CONFIRMED", unpaid_only=True, values={
        "paid_at": utcnow(),
        "payment_charge_id": charge,
        "provider_charge_id": payment.get("provider_payment_charge_id"),
        "paid_amount": payment["total_amount"] // MINOR,
    })
    db.commit()
    db.refresh(order)
    if not moved:
        log.error("Заказ %s изменился в момент оплаты %s: сейчас %s",
                  order.number, charge, order.status)
        return "late", order
    _invoices.pop(order.number, None)
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
        # Условно: между чтением и записью могла прийти оплата или открыться
        # окно оплаты — и то и другое проверяем в самой записи, а не по чтению
        if order_status.move(db, order, "CANCELED", unpaid_only=True, where=[
                or_(Order.checkout_at.is_(None),
                    Order.checkout_at <= now - CHECKOUT_GRACE)]):
            canceled.append(order.number)
            _invoices.pop(order.number, None)
    db.commit()
    if canceled:
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
