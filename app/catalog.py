"""API каталога для клиентского приложения."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Category, Product, Variant
from app.schemas import CategoryOut, ProductDetailOut, ProductListOut, VariantOut

router = APIRouter(prefix="/api", tags=["Каталог"])

PHOTO_PATH = "/static/img/"


def photo_url(filename: str | None) -> str | None:
    return f"{PHOTO_PATH}{filename}" if filename else None


def sellable_variants(product: Product) -> list[Variant]:
    """Фасовки, которые реально можно купить: опубликованы и с ценой."""
    return [v for v in product.variants if v.is_active and v.price is not None]


def sort_key(product: Product) -> str:
    """Порядок по алфавиту: без этого «Ё» уезжает вперёд всей кириллицы."""
    return product.name.lower().replace("ё", "е")


def load_sellable_products(db: Session, category_id: int | None = None) -> list[Product]:
    """Опубликованные товары, у которых есть хотя бы одна продаваемая фасовка.

    Карточка без единой фасовки с ценой покупателю бесполезна, поэтому в каталог
    она не попадает, даже если формально опубликована (уточнение к BR-34/BR-37).
    """
    stmt = select(Product).where(Product.is_active).options(selectinload(Product.variants))
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    return sorted((p for p in db.scalars(stmt) if sellable_variants(p)), key=sort_key)


@router.get("/categories", response_model=list[CategoryOut])
def get_categories(db: Session = Depends(get_db)):
    """Категории витрины с количеством доступных товаров."""
    counts: dict[int, int] = {}
    covers: dict[int, str] = {}
    for p in load_sellable_products(db):
        counts[p.category_id] = counts.get(p.category_id, 0) + 1
        if p.photo and p.category_id not in covers:
            covers[p.category_id] = p.photo

    categories = db.scalars(select(Category).order_by(Category.sort_order))
    return [
        CategoryOut(
            id=c.id,
            name=c.name,
            product_count=counts[c.id],
            photo=photo_url(covers.get(c.id)),
        )
        for c in categories
        if counts.get(c.id)
    ]


@router.get("/products", response_model=list[ProductListOut])
def get_products(category_id: int | None = None, db: Session = Depends(get_db)):
    """Список товаров каталога, при необходимости — по категории."""
    if category_id is not None and db.get(Category, category_id) is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    result = []
    for p in load_sellable_products(db, category_id):
        variants = sellable_variants(p)
        result.append(
            ProductListOut(
                id=p.id,
                name=p.name,
                photo=photo_url(p.photo),
                min_price=min(v.price for v in variants),
                weights=[v.weight for v in variants],
            )
        )
    return result


@router.get("/products/{product_id}", response_model=ProductDetailOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    """Карточка товара со списком доступных фасовок."""
    product = db.get(Product, product_id)
    variants = sellable_variants(product) if product and product.is_active else []
    if not variants:
        raise HTTPException(status_code=404, detail="Товар не найден")

    return ProductDetailOut(
        id=product.id,
        name=product.name,
        category=product.category.name,
        photo=photo_url(product.photo),
        description=product.description,
        composition=product.composition,
        shelf_life=product.shelf_life,
        variants=[VariantOut(id=v.id, weight=v.weight, price=v.price) for v in variants],
    )
