"""Данные покупателя: имя, телефон, адрес.

Хранятся отдельно от заказа, чтобы не набирать их заново каждый раз. В самом
заказе они всё равно дублируются на момент оформления (BR-09) — потом человек
может сменить адрес, а доставленный заказ должен помнить прежний.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Customer
from app.schemas import ContactIn, ProfileOut
from app.telegram import TelegramUser, require_user

router = APIRouter(prefix="/api", tags=["Профиль"])


def save_customer(
    db: Session, telegram_id: int, data: ContactIn, update_existing: bool = True
) -> Customer:
    """Заводит или обновляет покупателя. Без согласия ничего не сохраняем.

    update_existing=False — для заказа: разовая доставка на другой адрес, скажем
    подарок или работа, не должна навсегда переписывать профиль, тем более чужим
    телефоном. Профиль меняется только явным сохранением.
    """
    customer = db.get(Customer, telegram_id)
    if customer is None:
        if not data.consent:
            raise HTTPException(
                status_code=400,
                detail="Отметьте согласие на обработку данных — без него доставить заказ нельзя",
            )
        customer = Customer(telegram_id=telegram_id, consent_at=datetime.now(timezone.utc))
        db.add(customer)
    elif not update_existing:
        return customer

    customer.name = data.customer_name
    customer.phone = data.phone
    customer.address = data.address
    return customer


def to_out(customer: Customer | None, user: TelegramUser) -> ProfileOut:
    if customer is None:
        # имя знаем от Telegram, остальное спросим при первом заказе
        return ProfileOut(name=user.name, phone="", address="", consent=False)
    return ProfileOut(
        name=customer.name, phone=customer.phone, address=customer.address, consent=True
    )


@router.get("/profile", response_model=ProfileOut)
def get_profile(
    response: Response,
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_user),
):
    # ответ зависит от заголовка с подписью, а его промежуточные прокси
    # в ключ кэша не берут: чужой телефон мог бы прилететь не тому человеку
    response.headers["Cache-Control"] = "no-store"
    return to_out(db.get(Customer, user.id), user)


@router.put("/profile", response_model=ProfileOut)
def put_profile(
    data: ContactIn,
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_user),
):
    try:
        customer = save_customer(db, user.id, data)
        db.commit()
    except IntegrityError:
        # два сохранения пришли одновременно: запись уже завёл первый
        db.rollback()
        customer = save_customer(db, user.id, data)
        db.commit()
    return to_out(customer, user)


@router.delete("/profile", status_code=204)
def delete_profile(db: Session = Depends(get_db), user: TelegramUser = Depends(require_user)):
    """Отзыв согласия: имя, телефон и адрес удаляются.

    Оформленные заказы свою копию сохраняют — она нужна для учёта и относится
    к уже совершённой покупке, а не к дальнейшему хранению данных.
    """
    customer = db.get(Customer, user.id)
    if customer is not None:
        db.delete(customer)
        db.commit()
