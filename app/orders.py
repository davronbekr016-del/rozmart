"""Оформление заказа."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Order, OrderItem, Variant
from app.schemas import OrderIn, OrderItemOut, OrderOut

router = APIRouter(prefix="/api", tags=["Заказы"])

# BR-11: стоимость доставки фиксированная и единая для зоны обслуживания.
# Значение подтверждает заказчик; когда появится админка — переедет в настройки (FR-13.6).
DELIVERY_PRICE = 12000

# заказ с оплатой наличными сразу уходит в магазин, онлайн — ждёт подтверждения
# платежа и передаётся на сборку только после него (BR-13)
STATUS_BY_PAYMENT = {"cash": "CONFIRMED", "online": "NEW"}

# розница: больше сотни одинаковых пачек — это уже опт, такие заказы идут через оператора
MAX_ITEM_QUANTITY = 99


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
def create_order(data: OrderIn, db: Session = Depends(get_db)):
    """Создаёт заказ. Цены берутся из базы, а не из запроса клиента."""
    if data.client_key:
        already = find_by_client_key(db, data.client_key)
        if already is not None:
            return to_out(already)

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
        client_key=data.client_key,
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

    db.add(order)
    try:
        db.flush()                               # получаем id, чтобы собрать номер
        order.number = build_order_number(order.id)
        db.commit()
    except IntegrityError:
        # два одинаковых запроса пришли одновременно: заказ уже создал первый
        db.rollback()
        already = find_by_client_key(db, data.client_key) if data.client_key else None
        if already is None:
            raise
        return to_out(already)
    return to_out(order)


@router.get("/orders/{number}", response_model=OrderOut)
def get_order(number: str, db: Session = Depends(get_db)):
    order = db.scalar(
        select(Order).where(Order.number == number).options(selectinload(Order.items))
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return to_out(order)


@router.get("/delivery-price")
def delivery_price():
    """Стоимость доставки, чтобы клиент показывал итог до оформления."""
    return {"delivery_price": DELIVERY_PRICE}
