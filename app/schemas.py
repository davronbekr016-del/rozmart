"""Формат данных, которые API отдаёт клиентскому приложению.

Служебные поля учётной системы (код REGOS, складское наименование) сюда
намеренно не попадают — клиенту они не показываются (BR-36).
"""
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CategoryOut(BaseModel):
    id: int
    name: str
    product_count: int
    photo: str | None   # обложка: снимок первого товара категории


class VariantOut(BaseModel):
    id: int
    weight: str
    price: int


class ProductListOut(BaseModel):
    """Карточка в списке каталога: один товар, цена «от» (BR-37)."""

    id: int
    name: str
    photo: str | None
    min_price: int
    weights: list[str]


class ProductDetailOut(BaseModel):
    id: int
    name: str
    category: str
    photo: str | None
    description: str | None
    composition: str | None
    shelf_life: str | None
    variants: list[VariantOut]


# ---------- заказ ----------

DeliverySlot = Literal["Как можно скорее", "Сегодня вечером", "Завтра утром"]
PaymentMethod = Literal["cash", "online"]


class OrderItemIn(BaseModel):
    """Клиент присылает только что и сколько: цену считает сервер."""

    variant_id: int
    quantity: int = Field(ge=1, le=99)


class OrderIn(BaseModel):
    customer_name: str = Field(min_length=2, max_length=200)
    phone: str = Field(min_length=9, max_length=30)
    address: str = Field(min_length=5, max_length=500)
    delivery_slot: DeliverySlot
    payment_method: PaymentMethod
    comment: str | None = Field(default=None, max_length=500)
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    # ключ попытки оформления: повтор с тем же ключом вернёт уже созданный заказ
    client_key: str | None = Field(default=None, min_length=8, max_length=64)
    # итог, который видел покупатель. Разошёлся с расчётом сервера — не создаём
    # заказ молча по другой цене, а просим обновить корзину
    expected_total: int | None = Field(default=None, ge=0)

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str) -> str:
        digits = "".join(c for c in value if c.isdigit())
        if not digits.startswith("998") or len(digits) != 12:
            raise ValueError("Номер должен быть узбекским: +998 и 9 цифр")
        return f"+{digits}"

    @field_validator("customer_name", "address")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class OrderItemOut(BaseModel):
    product_name: str
    weight: str
    price: int
    quantity: int


class OrderOut(BaseModel):
    number: str
    status: str
    delivery_slot: str
    payment_method: str
    goods_total: int
    delivery_price: int
    total: int
    items: list[OrderItemOut]
