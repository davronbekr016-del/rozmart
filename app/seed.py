"""Наполнение пустой базы каталогом.

На сервере база создаётся с нуля: сам файл базы в репозиторий не едет, там
персональные данные покупателей. Каталог приезжает отдельным data/catalog.json,
который готовит scripts/export_catalog.py.
"""
import json
from pathlib import Path

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import Category, Product, Variant

SEED = Path(__file__).resolve().parent.parent / "data" / "catalog.json"


def seed_catalog() -> None:
    """Заливает каталог, если товаров ещё нет. Существующие не трогает."""
    if not SEED.exists():
        return

    db = SessionLocal()
    try:
        if db.scalar(select(Product.id).limit(1)) is not None:
            return

        data = json.loads(SEED.read_text(encoding="utf-8"))
        for row in data["categories"]:
            db.add(Category(**row))
        for row in data["products"]:
            variants = row.pop("variants")
            db.add(Product(**row))
            for variant in variants:
                db.add(Variant(product_id=row["id"], **variant))
        db.commit()
        _fix_sequences(db)
        print(f"Каталог залит: товаров {len(data['products'])}")
    finally:
        db.close()


def _fix_sequences(db) -> None:
    """Сдвигает счётчики id после заливки строк с явными идентификаторами.

    Каталог приезжает с проставленными id — так сохраняются ссылки между
    товарами и фасовками. PostgreSQL при вставке с явным id последовательность
    не двигает, и следующая запись, добавленная уже без id (например, новинка
    из REGOS), пытается занять id 1 и падает на конфликте первичного ключа.
    SQLite таким не страдает, поэтому на разработке проблема не проявлялась.
    """
    if db.bind.dialect.name != "postgresql":
        return
    for table in ("categories", "products", "variants"):
        db.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
        ))
    db.commit()
