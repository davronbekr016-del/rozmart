"""Узбекский в приложении магазина: что отвечает сервер — через HTTP.

    .venv\\Scripts\\python.exe tests/i18n_scenarios.py

Тексты экранов переводит само приложение (static/app.js); здесь — то, что
приходит от сервера: ошибки для покупателя, язык страницы Payme. База —
временный SQLite. Выход с кодом 1, если хоть одна проверка не прошла.
"""
import base64
import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import time
from urllib.parse import urlencode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp.as_posix()}",
    "BOT_TOKEN": "111:shop-token",
    "ADMIN_TELEGRAM_IDS": "1",
    "REGOS_KEY": "test-key",
    "REGOS_STOCK_ID": "5",
    "REGOS_PRICE_TYPE_ID": "5",
    "REGOS_ORDER_FROM_ID": "2",
    "PAYMENT_PROVIDER": "payme",
    "PAYME_MERCHANT_ID": "65f0c0ffee0000000000abcd",
    "PAYME_KEY": "sandbox-key-123",
    "PAYMENT_TEST_USERS": "900",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.orders_push as orders_push  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Category, Product, Variant  # noqa: E402

orders_push.push_one = lambda order_id: None

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=36000,
                  regos_price=36000, is_active=True)
hidden = Variant(product_id=prod.id, external_code="003101", weight="700 г", price=60000,
                 regos_price=60000, is_active=False)
db.add_all([variant, hidden]); db.commit()

client = TestClient(app)


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


BUYER = init_data(900)
UZ = {**BUYER, "X-Lang": "uz"}
results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def body(variant_id, key, payment="cash", expected=None):
    data = {"customer_name": "Javohir", "phone": "+998901234567", "address": "Chilonzor 9",
            "delivery_slot": "Как можно скорее", "payment_method": payment, "consent": True,
            "client_key": key, "items": [{"variant_id": variant_id, "quantity": 1}]}
    if expected is not None:
        data["expected_total"] = expected
    return data


r = client.get("/api/products/999", headers={"X-Lang": "uz"})
check(1, "Ошибка по-узбекски, если приложение на узбекском", r.json()["detail"] == "Mahsulot topilmadi")
r = client.get("/api/products/999")
check(1, "Без заголовка — по-русски, как раньше", r.json()["detail"] == "Товар не найден")
r = client.get("/api/products/999", headers={"X-Lang": "ru"})
check(1, "Русский — по-русски", r.json()["detail"] == "Товар не найден")

r = client.post("/api/orders", json=body(variant.id, "i18n-key-0001", expected=1), headers=UZ)
check(2, "«Цены изменились» — по-узбекски", r.status_code == 409
      and r.json()["detail"].startswith("Narxlar o'zgardi"), r.json())
r = client.post("/api/orders", json=body(hidden.id, "i18n-key-0002"), headers=UZ)
check(2, "Текст с названием товара — тоже", r.status_code == 400
      and r.json()["detail"] == "«Докторская» hozir buyurtma uchun mavjud emas", r.json())
r = client.get("/api/my-orders", headers={"X-Lang": "uz"})
check(2, "Без входа — «откройте из Telegram» по-узбекски", r.status_code == 401
      and "Telegram" in r.json()["detail"] and "oching" in r.json()["detail"], r.json())

r = client.post("/api/orders", json=body(variant.id, "i18n-key-0003", payment="online"), headers=UZ)
number = r.json()["number"]
link = client.post(f"/api/orders/{number}/invoice", headers=UZ).json()["link"]
check(3, "Страница Payme — на узбекском", ";l=uz" in base64.b64decode(link.rsplit("/", 1)[1]).decode())
link = client.post(f"/api/orders/{number}/invoice", headers=BUYER).json()["link"]
check(3, "Без заголовка — на русском", ";l=ru" in base64.b64decode(link.rsplit("/", 1)[1]).decode())

r = client.get("/api/admin/orders", headers={"X-Lang": "uz"})
check(4, "Статус ответа не меняется — только текст", r.status_code in (401, 404))

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
