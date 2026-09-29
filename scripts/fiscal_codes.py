"""Каких кодов для фискального чека не хватает у товаров витрины.

    .venv\\Scripts\\python.exe -m scripts.fiscal_codes

При оплате через Payme каждая строка чека уходит в налоговую с кодом МХИК
(ИКПУ), кодом упаковки и НДС. Коды приходят из REGOS с синхронизацией
каталога. Здесь — список опубликованных товаров, у которых чего-то нет:
его отдают бухгалтеру, он заполняет коды в REGOS, следующая синхронизация
приносит их на витрину. Код упаковки привязан к МХИК, его видно на
https://tasnif.soliq.uz/attribute/<МХИК>.
"""
from app.db import SessionLocal
from app.regos import prices


def main() -> int:
    db = SessionLocal()
    variants = prices.storefront_variants(db)
    missing = [v for v in variants if not v.mxik or not v.package_code or v.vat_percent is None]
    print(f"Фасовок на витрине: {len(variants)}, без полного набора кодов: {len(missing)}")
    for v in sorted(missing, key=lambda v: v.external_code):
        lacks = [name for name, value in (("МХИК", v.mxik), ("код упаковки", v.package_code),
                                          ("НДС", v.vat_percent)) if value in (None, "")]
        print(f"  {v.external_code}  {v.product.name} {v.weight}  "
              f"МХИК {v.mxik or '—'}  — нет: {', '.join(lacks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
