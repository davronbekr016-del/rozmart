"""Бот доставщиков — через HTTP к приложению.

    .venv\\Scripts\\python.exe tests/courier_scenarios.py

Telegram подменён: всё, что бот отправил бы, записывается здесь. База —
временный SQLite, REGOS не вызывается. Выход с кодом 1, если хоть одна
проверка не прошла.
"""
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
    "NOTIFY_BOT_TOKEN": "222:staff-token",
    "COURIER_BOT_TOKEN": "333:courier-token",
    "COURIER_WEBHOOK_SECRET": "courier-secret",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.orders_push as orders_push  # noqa: E402
from app import notify  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (Category, Courier, Notification, Order, Product,  # noqa: E402
                        StaffChat, Variant)

orders_push.push_one = lambda order_id: None   # в REGOS не ходим
orders_push.cancel_one = lambda order_id: None

sent = []   # (токен, метод, данные) — всё, что ушло бы в Telegram


def fake_call(method, payload, token=None):
    sent.append((token or "222:staff-token", method, payload))
    if method == "getMe":
        return {"username": "rozmart_courier_bot"}
    return {"message_id": len(sent)}


notify.call = fake_call

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=36000,
                  regos_price=36000, is_active=True)
db.add(variant)
db.add(StaffChat(chat_id=-100, title="Магазин", kind="group", active=True))
db.commit()

client = TestClient(app)
A, B = 501, 502          # два доставщика
SECRET = {"X-Telegram-Bot-Api-Secret-Token": "courier-secret"}


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Тест"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


ADMIN, BUYER = init_data(1), init_data(900)
results = []
update_id = [0]


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def hook(body, headers=SECRET):
    update_id[0] += 1
    return client.post("/tg/courier", json={"update_id": update_id[0], **body}, headers=headers)


def say(user, text):
    sent.clear()
    hook({"message": {"message_id": 1, "chat": {"id": user, "type": "private"},
                      "from": {"id": user, "first_name": f"Курьер{user}"}, "text": text}})
    return [p for t, m, p in sent if t == "333:courier-token"]


def press(user, data, message_id=77):
    sent.clear()
    r = hook({"callback_query": {"id": f"cb{update_id[0]}", "data": data,
                                 "from": {"id": user, "first_name": f"Курьер{user}"},
                                 "message": {"message_id": message_id,
                                             "chat": {"id": user, "type": "private"}}}})
    calls = [(m, p) for t, m, p in sent if t == "333:courier-token"]
    body = r.json()
    if body.get("method"):
        # ответ на нажатие — прямо в теле ответа на вебхук
        calls.insert(0, (body["method"], {k: v for k, v in body.items() if k != "method"}))
    return calls


def alert_text(calls):
    return next((p.get("text", "") for m, p in calls if m == "answerCallbackQuery"), "")


def edited(calls):
    return next((p for m, p in calls if m == "editMessageText"), None)


def order(key, payment="cash", lat=None):
    body = {"customer_name": "Жавохир", "phone": "+998901234567", "address": "Чиланзар 9, дом 4",
            "delivery_slot": "Как можно скорее", "payment_method": payment, "consent": True,
            "client_key": key, "items": [{"variant_id": variant.id, "quantity": 2}]}
    if lat:
        body.update({"lat": lat, "lon": 69.2})
    r = client.post("/api/orders", json=body, headers=BUYER)
    assert r.status_code == 201, r.text
    return r.json()["number"]


def row(number):
    s = SessionLocal()
    try:
        return s.query(Order).filter_by(number=number).one()
    finally:
        s.close()


def staff_texts():
    s = SessionLocal()
    try:
        return [n.text for n in s.query(Notification).order_by(Notification.id)]
    finally:
        s.close()


# ------------------------------------------------------------ доступ
r = hook({"message": {"chat": {"id": A, "type": "private"}, "from": {"id": A}, "text": "/start"}},
         headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
check(1, "Чужой секрет — обновление отвергнуто", r.json() == {"ok": False})
out = say(A, "/start")
check(1, "Незнакомому — «попросите администратора», заказов не видно",
      len(out) == 1 and "попросите администратора" in out[0]["text"])
s = SessionLocal()
check(1, "Человек запомнен без доступа", s.get(Courier, A).active is False)
s.close()
say(B, "/start")

cash1 = order("courier-key-001", lat=41.3)
calls = press(A, f"take:{row(cash1).id}")
check(2, "Без доступа заказ не взять", "Нет доступа" in alert_text(calls)
      and row(cash1).courier_id is None)
check(2, "Без доступа список заказов не показывается", "попросите" in say(A, "📦 Заказы")[0]["text"])

r = client.get("/api/admin/couriers", headers=ADMIN).json()
check(3, "Панель видит обоих доставщиков без доступа",
      {c["telegram_id"] for c in r["couriers"]} == {A, B}
      and not any(c["access"] for c in r["couriers"]) and r["bot"] == "rozmart_courier_bot")
check(3, "Покупатель список доставщиков не видит",
      client.get("/api/admin/couriers", headers=BUYER).status_code in (401, 404))
sent.clear()
client.post(f"/api/admin/couriers/{A}", headers=ADMIN)
client.post(f"/api/admin/couriers/{B}", headers=ADMIN)
welcome = [p for t, m, p in sent if t == "333:courier-token" and p.get("chat_id") == A]
check(3, "После доступа доставщику приходят кнопки «Заказы» и «Доставка»",
      welcome and welcome[0]["reply_markup"]["keyboard"] == [[{"text": "📦 Заказы"},
                                                             {"text": "🚚 Доставка"}]])

# ------------------------------------------------------------ список и захват
s = SessionLocal()
o = s.query(Order).filter_by(number=cash1).one()
new_unpaid = Order(number="RB-9999", status="NEW", telegram_id=900, customer_name="X",
                   phone="+998900000000", address="Где-то 1", delivery_slot="Как можно скорее",
                   payment_method="online", goods_total=1, delivery_price=0, total=1)
s.add(new_unpaid); s.commit(); s.close()

out = say(A, "📦 Заказы")
cards = [p for p in out if (p.get("reply_markup") or {}).get("inline_keyboard")]
check(4, "В «Заказах» свободный заказ с кнопкой «Принять заказ»",
      len(cards) == 1 and cash1 in cards[0]["text"]
      and cards[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Принять заказ")
check(4, "Неоплаченный заказ картой в списке не появляется",
      all("RB-9999" not in p["text"] for p in out))
check(4, "В карточке — сколько получить, телефон, адрес, товары",
      "Получить с покупателя" in cards[0]["text"] and "+998901234567" in cards[0]["text"]
      and "Чиланзар 9" in cards[0]["text"] and "Докторская 400 г × 2" in cards[0]["text"])

oid = row(cash1).id
calls = press(A, f"take:{oid}")
got = row(cash1)
check(5, "Принял — заказ «В доставке» и за ним", got.status == "DELIVERING"
      and got.courier_id == A and got.courier_prev_status == "CONFIRMED")
check(5, "Принятый заказ сразу исчезает из чата",
      ("deleteMessage", {"chat_id": A, "message_id": 77}) in calls and edited(calls) is None)
check(5, "Всплывает «принят — он в разделе Доставка»", "в разделе «🚚 Доставка»" in alert_text(calls))
delivery = [p for p in say(A, "🚚 Доставка") if (p.get("reply_markup") or {}).get("inline_keyboard")]
msg = delivery[0] if delivery else None
check(5, "В «Доставке» у него «Завершить», «Отменить» и карта",
      msg and [b["text"] for b in msg["reply_markup"]["inline_keyboard"][0]] == ["✔️ Завершить", "↩️ Отменить"]
      and msg["reply_markup"]["inline_keyboard"][1][0]["url"] == "https://maps.google.com/?q=41.3,69.2")
check(5, "Сотрудникам — кто взял заказ", any("взял доставщик Курьер501" in t for t in staff_texts()))

calls = press(B, f"take:{oid}")
check(6, "Второй доставщик тот же заказ не возьмёт", "другой доставщик" in alert_text(calls)
      and row(cash1).courier_id == A)
check(6, "И у него этот заказ исчезает из чата",
      ("deleteMessage", {"chat_id": B, "message_id": 77}) in calls)
check(6, "У второго в «Заказах» его больше нет",
      all(cash1 not in p["text"] for p in say(B, "📦 Заказы")))
check(6, "У первого он в «Доставке»", any(cash1 in p["text"] for p in say(A, "🚚 Доставка")))

# ------------------------------------------------------------ отказ
calls = press(A, f"drop:{oid}")
check(7, "«Отменить» сначала переспрашивает", row(cash1).courier_id == A
      and "Отказаться от заказа?" in edited(calls)["text"])
press(A, f"back:{oid}")
check(7, "«Назад» ничего не меняет", row(cash1).courier_id == A)
press(A, f"drop:{oid}")
calls = press(A, f"drop!:{oid}")
got = row(cash1)
check(7, "Отказался — заказ свободен и снова «Принят»", got.courier_id is None
      and got.status == "CONFIRMED" and got.courier_prev_status is None)
check(7, "Его снова видят все", any(cash1 in p["text"] for p in say(B, "📦 Заказы")))
check(7, "Сотрудникам — что доставщик отказался", any("отказался" in t for t in staff_texts()))

# ------------------------------------------------------------ завершение
press(B, f"take:{oid}")
calls = press(A, f"done!:{oid}")
check(8, "Чужой заказ завершить нельзя", "уже не у вас" in alert_text(calls)
      and row(cash1).status == "DELIVERING")
calls = press(B, f"done:{oid}")
check(8, "«Завершить» переспрашивает", "Покупатель получил заказ?" in edited(calls)["text"]
      and row(cash1).status == "DELIVERING")
press(B, f"done!:{oid}")
got = row(cash1)
check(8, "Завершил — заказ «Выполнен»", got.status == "DONE" and got.delivered_at is not None)
check(8, "Сотрудникам — доставлен и сколько наличными",
      any("доставлен" in t and "Получено наличными" in t for t in staff_texts()))
mine = next(x for x in client.get("/api/my-orders", headers=BUYER).json() if x["number"] == cash1)
check(8, "Покупатель видит «Выполнен»", mine["status"] == "DONE")
detail = client.get(f"/api/admin/orders/{oid}", headers=ADMIN).json()
check(8, "В панели видно, кто доставил", detail["courier_name"] == "Курьер502"
      and detail["delivered_at"])
check(8, "Выполненный в «Доставке» больше не висит",
      all(cash1 not in p["text"] for p in say(B, "🚚 Доставка")))

# ------------------------------------------------------------ отмена в панели
cash2 = order("courier-key-003")
o2 = row(cash2).id
press(A, f"take:{o2}")
sent.clear()
client.patch(f"/api/admin/orders/{o2}/status", json={"status": "CANCELED"}, headers=ADMIN)
told = [p for t, m, p in sent if t == "333:courier-token" and p.get("chat_id") == A]
check(9, "Отменили в панели — доставщику «не везите»", told and "отменён — не везите" in told[0]["text"])
calls = press(A, f"done!:{o2}")
check(9, "Отменённый не завершить", "уже не у вас" in alert_text(calls)
      and row(cash2).status == "CANCELED")

# ------------------------------------------------------------ касса уже провела продажу
cash3 = order("courier-key-004")
s = SessionLocal()
s.query(Order).filter_by(number=cash3).update({"status": "DELIVERING"}); s.commit(); s.close()
check(10, "«В доставке» без доставщика — свободен", any(cash3 in p["text"] for p in say(A, "📦 Заказы")))
o3 = row(cash3).id
press(A, f"take:{o3}")
press(A, f"drop!:{o3}")
check(10, "Отказ возвращает «В доставке» и свободным", row(cash3).status == "DELIVERING"
      and row(cash3).courier_id is None)

# ------------------------------------------------------------ отключение доставщика
press(B, f"take:{o3}")
r = client.delete(f"/api/admin/couriers/{B}", headers=ADMIN).json()
check(11, "Отключили — его заказы снова свободны", r["released"] == [cash3]
      and row(cash3).courier_id is None)
calls = press(B, f"take:{o3}")
check(11, "Отключённый больше ничего не берёт", "Нет доступа" in alert_text(calls)
      and row(cash3).courier_id is None)

# ------------------------------------------------------------ старше 48 часов
cash4 = order("courier-key-005")
real_call = notify.call


def old_message(method, payload, token=None):
    if method == "deleteMessage":
        raise notify.NotifyError("deleteMessage: 400 message can't be deleted")
    return real_call(method, payload, token)


notify.call = old_message
client.post(f"/api/admin/couriers/{B}", headers=ADMIN)
calls = press(B, f"take:{row(cash4).id}")
notify.call = real_call
msg = edited(calls)
check(12, "Не удалилось (старше 48 часов) — кнопки сняты, написано «вы приняли»",
      row(cash4).courier_id == B and msg and "Вы приняли заказ" in msg["text"]
      and "reply_markup" not in msg)

# ------------------------------------------------------------ скорость ответа
cash5 = order("courier-key-006")
sent.clear()
r = hook({"callback_query": {"id": "cb-fast", "data": f"take:{row(cash5).id}",
                             "from": {"id": B, "first_name": "Курьер502"},
                             "message": {"message_id": 90, "chat": {"id": B, "type": "private"}}}})
body = r.json()
check(13, "Ответ на нажатие — прямо в ответе вебхука, без отдельного запроса",
      body.get("method") == "answerCallbackQuery" and body.get("callback_query_id") == "cb-fast"
      and not any(m == "answerCallbackQuery" for t, m, p in sent))
order_of = [m for t, m, p in sent]
check(13, "Сначала убирается сообщение доставщика, потом — сотрудникам",
      bool(order_of) and order_of[0] == "deleteMessage", order_of)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
