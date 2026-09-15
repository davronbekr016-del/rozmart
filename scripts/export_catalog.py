"""Выгружает каталог в data/catalog.json.

База в репозиторий не едет: в ней телефоны и адреса покупателей. Каталог же
на сервере нужен — исходных Excel и фотографий из Google Диска там нет.
Поэтому он выгружается отдельным файлом, который к тому же видно в истории
изменений: понятно, когда и какая цена поменялась.

Запускать из корня проекта после каждого import_catalog.py:
    .venv\\Scripts\\python.exe -m scripts.export_catalog
"""
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Category, Product

SEED = Path(__file__).resolve().parent.parent / "data" / "catalog.json"


def main() -> None:
    db = SessionLocal()
    # идентификаторы сохраняем: фотографии названы по номеру товара
    data = {
        "categories": [
            {"id": c.id, "name": c.name, "sort_order": c.sort_order}
            for c in db.scalars(select(Category).order_by(Category.id))
        ],
        "products": [
            {
                "id": p.id,
                "category_id": p.category_id,
                "name": p.name,
                "description": p.description,
                "composition": p.composition,
                "shelf_life": p.shelf_life,
                "photo": p.photo,
                "is_active": p.is_active,
                "variants": [
                    {
                        "id": v.id,
                        "external_code": v.external_code,
                        "weight": v.weight,
                        "weight_grams": v.weight_grams,
                        "price": v.price,
                        "barcode": v.barcode,
                        "is_active": v.is_active,
                    }
                    for v in p.variants
                ],
            }
            for p in db.scalars(select(Product).order_by(Product.id))
        ],
    }
    db.close()

    SEED.parent.mkdir(parents=True, exist_ok=True)
    SEED.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    variants = sum(len(p["variants"]) for p in data["products"])
    print(f"{SEED}: категорий {len(data['categories'])}, товаров {len(data['products'])},"
          f" фасовок {variants}")


if __name__ == "__main__":
    main()
