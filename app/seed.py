"""Наполнение пустой базы каталогом.

На сервере база создаётся с нуля: сам файл базы в репозиторий не едет, там
персональные данные покупателей. Каталог приезжает отдельным data/catalog.json,
который готовит scripts/export_catalog.py.
"""
import json
from pathlib import Path

from sqlalchemy import select

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
        print(f"Каталог залит: товаров {len(data['products'])}")
    finally:
        db.close()
