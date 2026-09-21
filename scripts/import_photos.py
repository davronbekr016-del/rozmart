"""Подставляет фотографии товарам по коду REGOS.

В прототипе фотографии названы по коду номенклатуры: p-003098.jpg. Тот же код
лежит в Variant.external_code, поэтому фото раскладываются по карточкам
автоматически — вручную это восемь тысяч позиций перебирать.

Файл сохраняется под именем {product_id}.jpg: код REGOS — служебные данные
учётной системы, и в публичном адресе картинки ему делать нечего (BR-36).

Запуск:
    .venv\\Scripts\\python.exe -m scripts.import_photos <папка с img>
    .venv\\Scripts\\python.exe -m scripts.import_photos <папка> --dry-run
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import photos
from app.db import SessionLocal, init_db
from app.models import Product, Variant

DST = photos.DIR
NAME = re.compile(r"^p-0*(\d+)$", re.IGNORECASE)


def code_of(path: Path) -> str | None:
    """«p-003098.jpg» -> «003098». Файлы с именами не по коду пропускаем:
    в папке лежат ещё обложки категорий и фото-заглушки."""
    m = NAME.match(path.stem)
    return f"{int(m.group(1)):06d}" if m else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Раскладывает фото по кодам REGOS")
    parser.add_argument("source", help="папка с файлами p-XXXXXX.jpg")
    parser.add_argument("--dry-run", action="store_true", help="только показать")
    parser.add_argument("--overwrite", action="store_true",
                        help="заменять уже назначенные фото")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        print(f"Нет такой папки: {source}", file=sys.stderr)
        return 2

    files: dict[str, Path] = {}
    for path in source.iterdir():
        if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            if code := code_of(path):
                files[code] = path
    print(f"фотографий с кодом REGOS: {len(files)}")
    if not files:
        return 0

    init_db()
    db = SessionLocal()
    try:
        # одна карточка может собирать несколько фасовок: берём первое
        # подошедшее фото, остальные для неё уже не нужны
        assigned, skipped, missing = 0, 0, 0
        seen: set[int] = set()

        variants = db.scalars(
            select(Variant)
            .where(Variant.external_code.in_(files))
            .options(selectinload(Variant.product))
        ).all()
        found_codes = {v.external_code for v in variants}
        missing = len(files) - len(found_codes)

        for variant in variants:
            product = variant.product
            if product.id in seen:
                continue
            seen.add(product.id)
            if product.photo and not args.overwrite:
                skipped += 1
                continue

            src = files[variant.external_code]
            name = f"{product.id}{src.suffix.lower()}"
            if not args.dry_run:
                DST.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, DST / name)
                product.photo = name
            assigned += 1
            if assigned <= 10:
                print(f"  {variant.external_code} -> {product.name[:45]} -> {name}")

        if args.dry_run:
            db.rollback()
            print("(dry-run: ничего не записано)")
        else:
            db.commit()

        print(f"назначено {assigned}, пропущено (уже с фото) {skipped}, "
              f"кодов без товара в базе {missing}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
