from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Category(Base):
    """Категория витрины: копчёные колбасы, варёные, сосиски, деликатесы."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")


class Product(Base):
    """Витринная карточка товара. Цены и веса не содержит — они в фасовках."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT: категорию с товарами удалить нельзя — иначе товары осиротеют
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    composition: Mapped[str | None] = mapped_column(Text)
    shelf_life: Mapped[str | None] = mapped_column(String(100))
    photo: Mapped[str | None] = mapped_column(String(200))
    # BR-34: товар из REGOS создаётся скрытым, публикуется после заполнения названия и фото
    is_active: Mapped[bool] = mapped_column(default=False, server_default=false())

    category: Mapped["Category"] = relationship()
    variants: Mapped[list["Variant"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="Variant.weight_grams",
    )


class Variant(Base):
    """Фасовка товара: конкретный вес со своей ценой и кодом в REGOS."""

    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    external_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    weight: Mapped[str] = mapped_column(String(50))
    # для сортировки фасовок в карточке: «455 г» строкой сортируется бессмысленно
    weight_grams: Mapped[int] = mapped_column(default=0, server_default="0")
    # BR-10: сумы целыми числами. NULL допустим — часть позиций REGOS не имеет
    # цены до получения прайс-листа. Дефолта 0 нет намеренно: ноль исказил бы
    # «цену от» в каталоге (BR-37). Фасовка без цены не публикуется.
    price: Mapped[int | None]
    barcode: Mapped[str | None] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(default=False, server_default=false())

    product: Mapped["Product"] = relationship(back_populates="variants")


class Order(Base):
    """Заказ клиента. Статусы — по машине состояний из спецификации, раздел 3.2."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # BR-05: номер уникален и не меняется, его видит клиент и по нему ищет оператор
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20))
    # ключ попытки оформления с телефона клиента. Повторная отправка того же
    # заказа (двойной тап, обрыв связи, ретрай) вернёт уже созданный заказ.
    client_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    customer_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(Text)
    delivery_slot: Mapped[str] = mapped_column(String(60))
    comment: Mapped[str | None] = mapped_column(Text)
    payment_method: Mapped[str] = mapped_column(String(20))

    # BR-10: суммы целыми числами в сумах, зафиксированы на момент оформления
    goods_total: Mapped[int]
    delivery_price: Mapped[int]
    total: Mapped[int]

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    """Позиция заказа. Наименование и цена сохраняются на момент заказа (BR-09):
    последующее изменение каталога не должно менять уже оформленный заказ."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    # RESTRICT: фасовку, по которой есть заказы, удалять нельзя
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"))
    product_name: Mapped[str] = mapped_column(String(200))
    weight: Mapped[str] = mapped_column(String(50))
    price: Mapped[int]
    quantity: Mapped[int]

    order: Mapped["Order"] = relationship(back_populates="items")
