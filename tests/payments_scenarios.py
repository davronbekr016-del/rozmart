"""Сценарии ТЗ оплаты картой — через HTTP к приложению, с подписями Telegram.

    .venv\Scripts\python.exe tests/payments_scenarios.py

Номера проверок — номера сценариев из ТЗ «оплата картой (тест)»; 0 — доступ.
Telegram подменён: вместо запросов к api.telegram.org вызовы записываются.
База — отдельный временный SQLite, боевую не трогает. Выход с кодом 1,
если хоть одна проверка не прошла.

Чего здесь нет и быть не может: настоящего окна оплаты. Его проверяют руками
на телефоне тестовой картой — см. отчёт по ТЗ.
"""
import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
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

tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp.as_posix()}",
    "BOT_TOKEN": "111:shop-token",
    "NOTIFY_BOT_TOKEN": "222:notify-token",
    "PAYMENT_PROVIDER_TOKEN": "123456789:TEST:fake-provider-token",
    "PAYMENT_TEST_USERS": "900",
    "SHOP_WEBHOOK_SECRET": "shop-secret",
    "ADMIN_TELEGRAM_IDS": "1",
    "UNPAID_ORDER_MINUTES": "30",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import notify  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (Category, Notification, Order, Product, StaffChat,  # noqa: E402
                        Variant, utcnow)
from app import payments  # noqa: E402
from app.regos.orders_push import build_payload  # noqa: E402

calls = []


def fake_call(method, payload, token=None):
    calls.append((method, payload, token))
    if method == "createInvoiceLink":
        return f"https://t.me/$invoice-{payload['payload']}"
    return True


notify.call = fake_call

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Барбекю Бараньи", is_active=True)
db.add(prod); db.flush()
var = Variant(product_id=prod.id, weight="300 г", price=65000, is_active=True,
              external_code="003106")
db.add(var)
db.add(StaffChat(chat_id=777, title="Смена", kind="group", active=True))
db.commit()

client = TestClient(app)


def init_data(user_id, name="Тест"):
    user = json.dumps({"id": user_id, "first_name": name})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


BUYER, STRANGER, ADMIN = init_data(900), init_data(901), init_data(1)
results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + detail) if detail else ''}")


def order(payment, who=BUYER, key=None):
    body = {
        "customer_name": "Жавохир", "phone": "+998901234567", "address": "Чиланзар 9",
        "house": "15", "delivery_slot": "Как можно скорее", "payment_method": payment,
        "consent": True, "client_key": key or f"key-{time.time_ns()}",
        "items": [{"variant_id": var.id, "quantity": 1}],
    }
    return client.post("/api/orders", json=body, headers=who)


def hook(update, secret="shop-secret"):
    return client.post("/tg/shop", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def pre_checkout(number, amount, qid="q1"):
    calls.clear()
    hook({"update_id": 1, "pre_checkout_query": {
        "id": qid, "from": {"id": 900}, "currency": "UZS",
        "total_amount": amount, "invoice_payload": number}})
    answer = next(p for m, p, _ in calls if m == "answerPreCheckoutQuery")
    return answer


def paid(number, amount, charge="tg-charge-1"):
    return hook({"update_id": 2, "message": {
        "message_id": 5, "chat": {"id": 900, "type": "private"}, "from": {"id": 900},
        "successful_payment": {
            "currency": "UZS", "total_amount": amount, "invoice_payload": number,
            "telegram_payment_charge_id": charge,
            "provider_payment_charge_id": f"click-{charge}"}}})


def fresh(number):
    db.expire_all()
    return db.query(Order).filter_by(number=number).one()


def staff_texts(number):
    o = fresh(number)
    return [r.text for r in db.query(Notification).filter_by(order_id=o.id, kind="staff")]


# ---------------------------------------------------------------- проверки доступа
opts_buyer = client.get("/api/payment-options", headers=BUYER).json()
opts_stranger = client.get("/api/payment-options", headers=STRANGER).json()
check(0, "Карту видит только тестировщик",
      opts_buyer["card"] and not opts_stranger["card"],
      f"тестировщик {opts_buyer['card']}, посторонний {opts_stranger['card']}")
r = order("online", who=STRANGER)
check(0, "Посторонний не может оформить заказ картой (тестовый режим)",
      r.status_code == 400, r.json().get("detail", ""))
r = hook({"update_id": 9, "pre_checkout_query": {"id": "x"}}, secret="wrong")
check(0, "Вебхук с чужим секретом отклонён", r.json() == {"ok": False})

# ---------------------------------------------------------------- 1. оплата прошла
r = order("online"); o1 = r.json()
num = o1["number"]
check(1, "Заказ картой создан в «Ждёт оплаты», не оплачен",
      o1["status"] == "NEW" and not o1["paid"] and o1["pay_until"] is not None)
check(1, "До оплаты сотрудникам ничего не ушло", staff_texts(num) == [])
check(1, "До оплаты в REGOS не выгружен", fresh(num).regos_document_id is None)

calls.clear()
link = client.post(f"/api/orders/{num}/invoice", headers=BUYER)
inv = next(p for m, p, t in calls if m == "createInvoiceLink")
tok = next(t for m, p, t in calls if m == "createInvoiceLink")
check(1, "Счёт выставлен магазинным ботом с токеном провайдера",
      link.status_code == 200 and tok == "111:shop-token"
      and inv["provider_token"].startswith("123456789:TEST:") and inv["currency"] == "UZS")

ans = pre_checkout(num, 77000 * 100)
check(1, "Перед списанием сервер разрешил оплату", ans["ok"] is True)
check(1, "Окно оплаты помечено как открытое", fresh(num).checkout_at is not None)

paid(num, 77000 * 100)
o = fresh(num)
check(1, "После оплаты заказ «Принят», платёж сохранён",
      o.status == "CONFIRMED" and o.paid_at and o.payment_charge_id == "tg-charge-1"
      and o.provider_charge_id == "click-tg-charge-1" and o.paid_amount == 77000)
texts = staff_texts(num)
check(1, "Сотрудникам ушло сообщение с пометкой «оплачено, деньги не брать»",
      len(texts) == 1 and "ОПЛАЧЕНО КАРТОЙ — деньги с покупателя не брать" in texts[0]
      and "ТЕСТОВАЯ ОПЛАТА" in texts[0])
my = [x for x in client.get("/api/my-orders", headers=BUYER).json() if x["number"] == num][0]
check(1, "Покупатель видит заказ оплаченным", my["paid"] and my["status"] == "CONFIRMED"
      and my["pay_until"] is None)

# ---------------------------------------------------------------- 10. что увидит кассир
desc = build_payload(o, {var.id: "003106"})["document"]
check(10, "В REGOS: пометка первой строкой примечания, форма «Пласт. карта»",
      desc["description"].startswith("ТЕСТ! ОПЛАЧЕНО ОНЛАЙН (ТЕСТОВАЯ ОПЛАТА), ДЕНЬГИ НЕ БРАТЬ")
      and desc["payment_type_id"] == 2, desc["description"])

# ---------------------------------------------------------------- 2–3. закрыл окно / отказ
r = order("online"); n2 = r.json()["number"]
link1 = client.post(f"/api/orders/{n2}/invoice", headers=BUYER)
# окно закрыто без оплаты: ни pre_checkout, ни оплаты не было
link2 = client.post(f"/api/orders/{n2}/invoice", headers=BUYER)
check(2, "Закрыл окно — заказ ждёт оплаты, счёт можно открыть снова",
      fresh(n2).status == "NEW" and link1.status_code == 200 and link2.status_code == 200)
ans = pre_checkout(n2, 77000 * 100, qid="q-fail")
# банк отказал: pre_checkout был, successful_payment — нет
check(3, "Оплата не прошла — заказ по-прежнему ждёт оплаты",
      ans["ok"] and fresh(n2).status == "NEW" and fresh(n2).paid_at is None)

# ---------------------------------------------------------------- 4. дубль уведомления
before = len(staff_texts(num))
paid(num, 77000 * 100)                         # то же уведомление второй раз
o = fresh(num)
check(4, "Повтор уведомления об оплате ничего не меняет",
      o.status == "CONFIRMED" and len(staff_texts(num)) == before)

# ---------------------------------------------------------------- 5. сумма
total_in_invoice = sum(p["amount"] for p in inv["prices"])
check(5, "Сумма в счёте равна заказу с доставкой, до тийина",
      total_in_invoice == 77000 * 100
      and inv["prices"][-1] == {"label": "Доставка", "amount": 12000 * 100},
      f"{total_in_invoice} тийинов, строки: {[p['label'] for p in inv['prices']]}")
ans = pre_checkout(n2, 76000 * 100, qid="q-amount")
check(5, "Другая сумма в окне оплаты — отказ", ans["ok"] is False, ans.get("error_message", ""))

# ---------------------------------------------------------------- 6. отменили при открытом окне
r = order("online"); n6 = r.json()["number"]
o6 = fresh(n6)
client.patch(f"/api/admin/orders/{o6.id}/status", json={"status": "CANCELED"}, headers=ADMIN)
ans = pre_checkout(n6, 77000 * 100, qid="q-cancel")
check(6, "Заказ отменили — оплата отклонена", ans["ok"] is False, ans.get("error_message", ""))
# гонка: списание уже шло, когда заказ отменили
paid(n6, 77000 * 100, charge="tg-late")
texts = staff_texts(n6)
check(6, "Деньги пришли за отменённый — заказ не воскрес, сотрудникам «нужен возврат»",
      fresh(n6).status == "CANCELED" and any("Нужен возврат" in t for t in texts))

# ---------------------------------------------------------------- 7. не оплатил 30 минут
r = order("online"); n7 = r.json()["number"]
r = order("online"); n7b = r.json()["number"]
old = utcnow() - timedelta(minutes=31)
# оба заказа берём до правок: fresh() сбрасывает несохранённые изменения
o7, o7b = fresh(n7), fresh(n7b)
o7.created_at = old
o7b.created_at = old
o7b.checkout_at = utcnow() - timedelta(minutes=1)
db.commit()
canceled = payments.cancel_stale(SessionLocal())
check(7, "Не оплатил 30 минут — заказ отменён, в REGOS не ушёл",
      fresh(n7).status == "CANCELED" and fresh(n7).regos_document_id is None)
check(7, "Но если окно оплаты открыто — не отменяем",
      fresh(n7b).status == "NEW", f"отменены: {canceled}")

# ---------------------------------------------------------------- 8. ручное подтверждение
o8 = fresh(n2)
r = client.patch(f"/api/admin/orders/{o8.id}/status", json={"status": "CONFIRMED"},
                 headers=ADMIN)
detail = client.get(f"/api/admin/orders/{o8.id}", headers=ADMIN).json()
check(8, "Оператор не может вручную подтвердить неоплаченный заказ",
      r.status_code == 409 and fresh(n2).status == "NEW" and detail["awaiting_payment"],
      r.json().get("detail", ""))
pd = client.get(f"/api/admin/orders/{fresh(num).id}", headers=ADMIN).json()
check(8, "В карточке панели: когда оплачен, сумма, номер платежа",
      pd["paid_at"] and pd["paid_amount"] == 77000 and pd["provider_charge_id"] == "click-tg-charge-1")

# ---------------------------------------------------------------- 9. наличные как раньше
r = order("cash"); o9 = r.json()
texts = staff_texts(o9["number"])
inv9 = client.post(f"/api/orders/{o9['number']}/invoice", headers=BUYER)
check(9, "Заказ за наличные — «Принят» сразу, сотрудникам сразу, счёта нет",
      o9["status"] == "CONFIRMED" and len(texts) == 1 and "💵 наличными курьеру" in texts[0]
      and inv9.status_code == 409)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
