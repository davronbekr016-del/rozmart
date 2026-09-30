"""Оплата через Payme Merchant API — через HTTP к приложению.

    .venv\\Scripts\\python.exe tests/payme_scenarios.py

Запросы Payme подделываются здесь же: те же методы и те же проверки, что
прогоняет песочница Payme (test.paycom.uz) — неверный ключ, чужая сумма,
повторы, отмена до и после проведения, таймаут. База — временный SQLite,
REGOS не вызывается. Выход с кодом 1, если хоть одна проверка не прошла.

Страницу оплаты Payme здесь не проверить — её проходят руками в песочнице.
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
from datetime import timedelta
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
    "PAYME_TEST": "1",
    "PAYME_RETURN_URL": "https://t.me/rozmartuz_bot",
    "PAYMENT_TEST_USERS": "900",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.orders_push as orders_push  # noqa: E402
from app import payments  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Category, Order, PaymeTransaction, Product, Variant, utcnow  # noqa: E402
from app.regos.catalog_sync import apply_fiscal  # noqa: E402

# в REGOS не ходим: только запоминаем, что заказ ушёл бы туда
pushed = []
orders_push.push_one = lambda order_id: pushed.append(order_id)

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Барбекю Бараньи", is_active=True)
db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003106", weight="300 г", price=65000,
                  regos_price=65000, is_active=True, mxik="10202001001000000",
                  package_code="1399448", vat_percent=12)
db.add(variant); db.commit()

client = TestClient(app)


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Тест"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


TESTER, STRANGER = init_data(900), init_data(901)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"Paycom:sandbox-key-123").decode()}
results = []
seq = [0]


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def rpc(method, params, headers=AUTH):
    seq[0] += 1
    r = client.post("/payme", json={"jsonrpc": "2.0", "id": seq[0], "method": method,
                                    "params": params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def code(resp):
    return (resp.get("error") or {}).get("code")


def new_order(key, payment="online", headers=TESTER):
    body = {"customer_name": "Жавохир", "phone": "+998901234567", "address": "Чиланзар 9",
            "delivery_slot": "Как можно скорее", "payment_method": payment, "consent": True,
            "client_key": key, "items": [{"variant_id": variant.id, "quantity": 2}]}
    return client.post("/api/orders", json=body, headers=headers)


def order_row(number):
    s = SessionLocal()
    try:
        return s.query(Order).filter_by(number=number).one()
    finally:
        s.close()


def tx_row(payme_id):
    s = SessionLocal()
    try:
        return s.query(PaymeTransaction).filter_by(payme_id=payme_id).one()
    finally:
        s.close()


def shift(model, where, **values):
    """Сдвигает время в базе — чтобы проверить таймауты без ожидания."""
    s = SessionLocal()
    s.query(model).filter_by(**where).update(values)
    s.commit()
    s.close()


def acc(number):
    return {"order_id": number}


now_ms = lambda: int(time.time() * 1000)  # noqa: E731

# ------------------------------------------------------------ доступ
r = rpc("CheckPerformTransaction", {"amount": 1, "account": acc("x")}, headers={})
check(1, "Без ключа — отказ -32504", code(r) == -32504, code(r))
bad = {"Authorization": "Basic " + base64.b64encode(b"Paycom:wrong").decode()}
check(1, "Чужой ключ — отказ -32504", code(rpc("CheckTransaction", {"id": "a"}, bad)) == -32504)
bad = {"Authorization": "Basic " + base64.b64encode(b"Other:sandbox-key-123").decode()}
check(1, "Чужой логин — отказ -32504", code(rpc("CheckTransaction", {"id": "a"}, bad)) == -32504)
check(2, "Неизвестный метод — -32601", code(rpc("ChangePassword", {"password": "x"})) == -32601)
r = client.post("/payme", content=b"{oops", headers=AUTH).json()
check(2, "Кривой JSON — -32700", code(r) == -32700)

# ------------------------------------------------------------ заказ и ссылка
r = new_order("pm-key-001")
o1 = r.json()
check(3, "Тестировщик оформляет заказ картой", r.status_code == 201 and o1["status"] == "NEW")
check(3, "Остальным оплата картой недоступна", new_order("pm-key-x", headers=STRANGER).status_code == 400)
opts = client.get("/api/payment-options", headers=TESTER).json()
check(3, "Приложение знает, что платить через Payme", opts["provider"] == "payme" and opts["card"])

r = client.post(f"/api/orders/{o1['number']}/invoice", headers=TESTER).json()
decoded = base64.b64decode(r["link"].rsplit("/", 1)[1]).decode()
amount1 = o1["total"] * 100
check(4, "Ссылка ведёт на песочницу Payme", r["kind"] == "payme"
      and r["link"].startswith("https://test.paycom.uz/"), r["link"])
check(4, "В ссылке касса, номер заказа, сумма в тийинах и возврат в бота",
      decoded == f"m=65f0c0ffee0000000000abcd;ac.order_id={o1['number']};a={amount1};l=ru;"
                 "c=https://t.me/rozmartuz_bot", decoded)
check(4, "Чужой заказ — 404", client.post(f"/api/orders/{o1['number']}/invoice",
                                         headers=STRANGER).status_code == 404)

# ------------------------------------------------------------ CheckPerformTransaction
check(5, "Не та сумма — -31001",
      code(rpc("CheckPerformTransaction", {"amount": amount1 - 100, "account": acc(o1["number"])})) == -31001)
r = rpc("CheckPerformTransaction", {"amount": amount1, "account": acc("RB-0000")})
check(5, "Нет такого заказа — ошибка по заказу с полем order_id",
      -31099 <= code(r) <= -31050 and r["error"]["data"] == "order_id", r.get("error"))
r = rpc("CheckPerformTransaction", {"amount": amount1, "account": acc(o1["number"])})
detail = (r.get("result") or {}).get("detail") or {}
items = detail.get("items") or [{}]
total = sum(i["price"] * i["count"] for i in items) + (detail.get("shipping") or {}).get("price", 0)
check(5, "Оплатить можно", (r.get("result") or {}).get("allow") is True, r)
check(5, "Чек сходится с суммой до тийина", total == amount1, f"{total} / {amount1}")
check(5, "В чеке МХИК, код упаковки и НДС",
      items[0].get("code") == "10202001001000000" and items[0].get("package_code") == "1399448"
      and items[0].get("vat_percent") == 12 and items[0].get("count") == 2, items[0])
check(5, "Доставка в чеке отдельной строкой", detail.get("shipping", {}).get("title") == "Доставка")
cash = new_order("pm-key-cash", payment="cash").json()
check(5, "Наличный заказ картой не оплатить",
      code(rpc("CheckPerformTransaction", {"amount": cash["total"] * 100,
                                           "account": acc(cash["number"])})) == -31051)

# ------------------------------------------------------------ CreateTransaction
t = now_ms()
r1 = rpc("CreateTransaction", {"id": "tx-A", "time": t, "amount": amount1, "account": acc(o1["number"])})
check(6, "Транзакция создана, состояние 1", (r1.get("result") or {}).get("state") == 1, r1)
r2 = rpc("CreateTransaction", {"id": "tx-A", "time": t, "amount": amount1, "account": acc(o1["number"])})
check(6, "Повтор того же запроса — тот же ответ", r2.get("result") == r1.get("result"))
r = rpc("CreateTransaction", {"id": "tx-B", "time": t, "amount": amount1, "account": acc(o1["number"])})
check(6, "Вторая оплата того же заказа, пока идёт первая, — отказ",
      -31099 <= (code(r) or 0) <= -31050, r.get("error"))
check(6, "Сумма не та — -31001", code(rpc("CreateTransaction", {
    "id": "tx-Z", "time": t, "amount": 1, "account": acc(o1["number"])})) == -31001)
check(6, "Заказ ещё не оплачен", order_row(o1["number"]).paid_at is None)

# ------------------------------------------------------------ PerformTransaction
pushed.clear()   # выгрузка при оформлении — не наша: неоплаченный NEW она пропускает
r = rpc("PerformTransaction", {"id": "tx-A"})
order = order_row(o1["number"])
check(7, "Проведена, состояние 2", (r.get("result") or {}).get("state") == 2, r)
check(7, "Заказ оплачен и принят", order.status == "CONFIRMED" and order.paid_at is not None
      and order.paid_amount == o1["total"] and order.provider_charge_id == "tx-A")
check(7, "Заказ ушёл в REGOS", pushed == [order.id], pushed)
r2 = rpc("PerformTransaction", {"id": "tx-A"})
check(7, "Повтор — тот же ответ и без второй выгрузки",
      r2.get("result") == r.get("result") and pushed == [order.id])
check(7, "Нет такой транзакции — -31003", code(rpc("PerformTransaction", {"id": "nope"})) == -31003)
r = rpc("CheckTransaction", {"id": "tx-A"})["result"]
check(8, "CheckTransaction видит проведённую", r["state"] == 2 and r["perform_time"] > 0
      and r["reason"] is None)
check(8, "Оплаченный заказ второй раз не оплатить", code(rpc("CheckPerformTransaction", {
    "amount": amount1, "account": acc(o1["number"])})) == -31052)
mine = next(x for x in client.get("/api/my-orders", headers=TESTER).json()
            if x["number"] == o1["number"])
check(8, "Покупатель видит «оплачено»", mine["paid"] is True)

# ------------------------------------------------------------ возврат
r = rpc("CancelTransaction", {"id": "tx-A", "reason": 5})
check(9, "Возврат: состояние -2", (r.get("result") or {}).get("state") == -2, r)
check(9, "Заказ отменён — выдавать нельзя", order_row(o1["number"]).status == "CANCELED")
check(9, "Повтор отмены — тот же ответ", rpc("CancelTransaction", {"id": "tx-A", "reason": 5})
      .get("result") == r.get("result"))
check(9, "Отменённую не провести", code(rpc("PerformTransaction", {"id": "tx-A"})) == -31008)

# ------------------------------------------------------------ бросил оплату
o2 = new_order("pm-key-002").json()
a2 = o2["total"] * 100
rpc("CreateTransaction", {"id": "tx-C", "time": now_ms(), "amount": a2, "account": acc(o2["number"])})
r = rpc("CancelTransaction", {"id": "tx-C", "reason": 3})
check(10, "Отмена до проведения: состояние -1", (r.get("result") or {}).get("state") == -1)
check(10, "Заказ по-прежнему ждёт оплаты", order_row(o2["number"]).status == "NEW")
check(10, "Отменённую не провести", code(rpc("PerformTransaction", {"id": "tx-C"})) == -31008)
r = rpc("CreateTransaction", {"id": "tx-D", "time": now_ms(), "amount": a2, "account": acc(o2["number"])})
check(10, "Можно заплатить заново", (r.get("result") or {}).get("state") == 1, r)

# брошенная давно — не мешает новой оплате
shift(PaymeTransaction, {"payme_id": "tx-D"}, create_time=now_ms() - 20 * 60 * 1000)
r = rpc("CreateTransaction", {"id": "tx-E", "time": now_ms(), "amount": a2, "account": acc(o2["number"])})
check(11, "Оплата, брошенная 20 минут назад, не держит заказ",
      (r.get("result") or {}).get("state") == 1 and tx_row("tx-D").state == -1, r)
rpc("PerformTransaction", {"id": "tx-E"})
check(11, "Новая оплата проведена", order_row(o2["number"]).paid_at is not None)
check(11, "Брошенную Payme уже не проведёт", code(rpc("PerformTransaction", {"id": "tx-D"})) == -31008)

# ------------------------------------------------------------ таймаут 12 часов
o3 = new_order("pm-key-003").json()
a3 = o3["total"] * 100
rpc("CreateTransaction", {"id": "tx-F", "time": now_ms(), "amount": a3, "account": acc(o3["number"])})
shift(PaymeTransaction, {"payme_id": "tx-F"}, create_time=now_ms() - 13 * 3600 * 1000)
check(12, "Через 12 часов провести нельзя", code(rpc("PerformTransaction", {"id": "tx-F"})) == -31008)
f = tx_row("tx-F")
check(12, "Транзакция отменена по таймауту (причина 4)", f.state == -1 and f.reason == 4)

# ------------------------------------------------------------ автоотмена и оплата
o4 = new_order("pm-key-004").json()
a4 = o4["total"] * 100
rpc("CreateTransaction", {"id": "tx-G", "time": now_ms(), "amount": a4, "account": acc(o4["number"])})
shift(Order, {"number": o4["number"]}, created_at=utcnow() - timedelta(hours=1))
payments.cancel_stale(SessionLocal())
check(13, "Пока идёт оплата, автоотмена заказ не трогает", order_row(o4["number"]).status == "NEW")
shift(Order, {"number": o4["number"]}, checkout_at=utcnow() - timedelta(hours=1))
payments.cancel_stale(SessionLocal())
check(13, "Брошенный заказ автоотмена отменяет", order_row(o4["number"]).status == "CANCELED")
check(13, "Оплату отменённого заказа не провести — деньги вернутся",
      code(rpc("PerformTransaction", {"id": "tx-G"})) == -31008 and tx_row("tx-G").state == -1)
check(13, "Отменённый заказ не оплачен", order_row(o4["number"]).paid_at is None)

# ------------------------------------------------------------ GetStatement
r = rpc("GetStatement", {"from": now_ms() - 3600 * 1000, "to": now_ms() + 1000})
ids = [x["id"] for x in r["result"]["transactions"]]
check(14, "Выписка за час — все транзакции", {"tx-A", "tx-C", "tx-D", "tx-E", "tx-F", "tx-G"} <= set(ids), ids)
row = next(x for x in r["result"]["transactions"] if x["id"] == "tx-A")
check(14, "В выписке номер заказа и состояние", row["account"] == {"order_id": o1["number"]}
      and row["state"] == -2)

# ------------------------------------------------------------ синхронизация кодов из REGOS
v = Variant(product_id=prod.id, external_code="009999", weight="1 кг", price=1)
apply_fiscal(v, {"icps": "01601001001000000", "package_code": "1399448",
                 "vat": {"value": 12.0, "enabled": True}})
check(15, "Коды для чека берутся из REGOS", v.mxik == "01601001001000000"
      and v.package_code == "1399448" and v.vat_percent == 12)
apply_fiscal(v, {"icps": "01601001001000000", "package_code": None, "vat": {"value": 12.0}})
check(15, "Пустой код упаковки из REGOS не затирает известный", v.package_code == "1399448")

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
