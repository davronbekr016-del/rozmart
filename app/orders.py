"""Оформление заказа."""
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Order, OrderItem, Variant
from app.profile import save_customer
from app.schemas import OrderIn, OrderItemOut, OrderOut
from app.telegram import TelegramUser, buyer, require_user

router = APIRouter(prefix="/api", tags=["Заказы"])

# BR-11: стоимость доставки фиксированная и единая для зоны обслуживания.
# Значение подтверждает заказчик; когда появится админка — переедет в настройки (FR-13.6).
DELIVERY_PRICE = 12000

# заказ с оплатой наличными сразу уходит в магазин, онлайн — ждёт подтверждения
# платежа и передаётся на сборку только после него (BR-13)
STATUS_BY_PAYMENT = {"cash": "CONFIRMED", "online": "NEW"}

# розница: больше сотни одинаковых пачек — это уже опт, такие заказы идут через оператора
MAX_ITEM_QUANTITY = 99

# служебный код состояния покупателю ни о чём не говорит. Остальные состояния
# из спецификации появятся вместе с рабочим местом оператора, который их ставит
STATUS_TEXT = {"NEW": "Ждёт оплаты", "CONFIRMED": "Принят"}


def build_order_number(order_id: int) -> str:
    """RB-8001, RB-8002 … Номер присваивается один раз и не меняется (BR-05)."""
    return f"RB-{8000 + order_id}"


def find_by_client_key(db: Session, client_key: str) -> Order | None:
    return db.scalar(
        select(Order).where(Order.client_key == client_key).options(selectinload(Order.items))
    )


def to_out(order: Order) -> OrderOut:
    return OrderOut(
        number=order.number,
        status=order.status,
        status_text=STATUS_TEXT.get(order.status, "В работе"),
        # в базе время без пояса, но оно всегда UTC: помечаем явно, иначе
        # телефон покажет его как местное и промахнётся на пять часов
        created_at=order.created_at.replace(tzinfo=timezone.utc),
        delivery_slot=order.delivery_slot,
        payment_method=order.payment_method,
        goods_total=order.goods_total,
        delivery_price=order.delivery_price,
        total=order.total,
        items=[
            OrderItemOut(
                product_name=i.product_name, weight=i.weight, price=i.price, quantity=i.quantity
            )
            for i in order.items
        ],
    )


@router.post("/orders", response_model=OrderOut, status_code=201)
def create_order(
    data: OrderIn,
    db: Session = Depends(get_db),
    user: TelegramUser | None = Depends(buyer),
):
    """Создаёт заказ. Цены берутся из базы, а не из запроса клиента."""
    telegram_id = user.id if user else None
    client_key = data.client_key
    if client_key:
        already = find_by_client_key(db, client_key)
        if already is not None:
            # свой ключ — возвращаем тот же заказ вместо второго такого же.
            # чужой — просто не занимаем его: подставив украденный ключ, чужой
            # заказ не прочитать, а свой оформить можно
            if already.telegram_id == telegram_id:
                return to_out(already)
            client_key = None

    quantities: dict[int, int] = {}
    for item in data.items:
        quantities[item.variant_id] = quantities.get(item.variant_id, 0) + item.quantity

    # ограничение из схемы — на строку, а одинаковые строки мы складываем
    if any(quantity > MAX_ITEM_QUANTITY for quantity in quantities.values()):
        raise HTTPException(
            status_code=400,
            detail=f"Не больше {MAX_ITEM_QUANTITY} штук одной позиции в заказе",
        )

    variants = db.scalars(
        select(Variant)
        .where(Variant.id.in_(quantities))
        .options(selectinload(Variant.product))
    ).all()

    found = {v.id for v in variants}
    missing = set(quantities) - found
    if missing:
        raise HTTPException(status_code=400, detail="В корзине есть товары, которых больше нет")

    order = Order(
        number="",
        status=STATUS_BY_PAYMENT[data.payment_method],
        client_key=client_key,
        telegram_id=telegram_id,
        customer_name=data.customer_name,
        phone=data.phone,
        address=data.address,
        delivery_slot=data.delivery_slot,
        comment=data.comment,
        payment_method=data.payment_method,
        goods_total=0,
        delivery_price=DELIVERY_PRICE,
        total=0,
    )

    goods_total = 0
    for variant in variants:
        # проданной считается только опубликованная фасовка с ценой: иначе можно
        # оформить скрытый товар, зная его номер
        if not variant.is_active or variant.price is None or not variant.product.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"«{variant.product.name}» сейчас недоступен для заказа",
            )
        quantity = quantities[variant.id]
        goods_total += variant.price * quantity
        order.items.append(
            OrderItem(
                variant_id=variant.id,
                product_name=variant.product.name,
                weight=variant.weight,
                price=variant.price,
                quantity=quantity,
            )
        )

    order.goods_total = goods_total
    order.total = goods_total + DELIVERY_PRICE

    # покупатель подтверждает конкретную сумму: если каталог успел измениться,
    # оформляем не молча по новой цене, а возвращаем его в корзину
    if data.expected_total is not None and data.expected_total != order.total:
        raise HTTPException(
            status_code=409,
            detail="Цены изменились. Проверьте корзину и подтвердите заказ заново.",
        )

    # запоминаем покупателя, чтобы в следующий раз не набирал то же самое.
    # Одной транзакцией с заказом: заказ без согласия на обработку не создаётся.
    # Уже сохранённый профиль заказ не трогает — его меняют только вручную
    if user:
        save_customer(db, user.id, data, update_existing=False)

    db.add(order)
    try:
        db.flush()                               # получаем id, чтобы собрать номер
        order.number = build_order_number(order.id)
        db.commit()
    except IntegrityError:
        # два одинаковых запроса пришли одновременно: заказ уже создал первый
        db.rollback()
        already = find_by_client_key(db, client_key) if client_key else None
        if already is None or already.telegram_id != telegram_id:
            raise
        return to_out(already)
    return to_out(order)


@router.get("/my-orders", response_model=list[OrderOut])
def my_orders(
    response: Response,
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_user),
):
    """Заказы этого покупателя, свежие сверху."""
    # ответ зависит от заголовка с подписью, промежуточные прокси его не учтут
    response.headers["Cache-Control"] = "no-store"
    orders = db.scalars(
        select(Order)
        .where(Order.telegram_id == user.id)
        .order_by(Order.id.desc())
        .limit(20)
        .options(selectinload(Order.items))
    )
    return [to_out(order) for order in orders]


@router.get("/delivery-price")
def delivery_price():
    """Стоимость доставки, чтобы клиент показывал итог до оформления."""
    return {"delivery_price": DELIVERY_PRICE}
