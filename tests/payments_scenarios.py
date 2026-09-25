"""Сценарии ТЗ оплаты картой — через HTTP к приложению, с подписями Telegram.

    .venv\Scripts\python.exe tests/payments_scenarios.py

Номера проверок — номера сценариев из ТЗ «оплата картой (тест)»; 0 — доступ.
Telegram подменён: вместо запросов к api.telegram.org вызовы записываются.
База — отдельный временный SQLite, боевую не трогает. Выход с кодом 1,
если хоть одна проверка не прошла.

Чего здесь нет и быть не может: настоящего окна оплаты. Его проверяют руками
на телефоне. Нужна тестовая карта Click — её выдаёт сам Click (поддержка или
менеджер, подключавший CLICK Terminal Test). В открытой документации Click её нет,
в репозитории и у программиста — тоже. Настоящую карту тестовый терминал
не проведёт: он не сможет отправить код подтверждения.
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
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {str(n):>3}. {name}{(' — ' + detail) if detail else ''}")


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
    # ответ Telegram получает прямо в теле ответа на вебхук
    answer = hook({"update_id": 1, "pre_checkout_query": {
        "id": qid, "from": {"id": 900}, "currency": "UZS",
        "total_amount": amount, "invoice_payload": number}}).json()
    assert answer.get("method") == "answerPreCheckoutQuery", answer
    assert answer.get("pre_checkout_query_id") == qid, answer
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

# ======================================================= находки независимого ревью
from app import order_status  # noqa: E402
from app.regos.orders_sync import target_status  # noqa: E402

# 1. ручная выгрузка неоплаченного в REGOS и синхронизация с кассой
r = order("online"); nr1 = r.json()["number"]
o = fresh(nr1)
r = client.post(f"/api/admin/orders/{o.id}/push", headers=ADMIN)
check("R1", "Кнопка «Отправить в REGOS» не выгружает неоплаченный заказ картой",
      r.status_code == 409 and fresh(nr1).regos_document_id is None, r.json().get("detail", ""))
check("R1", "«Утверждён» с кассы не делает неоплаченный заказ «Принятым»",
      target_status(fresh(nr1), {"id": 23, "name": "Утвержден"}) is None
      and target_status(fresh(nr1), {"id": 27, "name": "Отменен"}) == "CANCELED")

# 2. сбой при проведении платежа — 500, повтор проводит
import app.payments as _pay  # noqa: E402
real_record = _pay.record_payment
from sqlalchemy.exc import OperationalError  # noqa: E402
def broken(*a, **kw):
    raise OperationalError("UPDATE orders", {}, Exception("база недоступна"))
_pay.record_payment = broken
r = order("online"); nr2 = r.json()["number"]
resp = paid(nr2, 77000 * 100, charge="tg-retry")
_pay.record_payment = real_record
check("R2", "Сбой при проведении платежа — вебхук отвечает 500, Telegram повторит",
      resp.status_code == 500 and fresh(nr2).paid_at is None)
resp = paid(nr2, 77000 * 100, charge="tg-retry")
check("R2", "Повтор уведомления проводит платёж",
      resp.status_code == 200 and fresh(nr2).status == "CONFIRMED" and fresh(nr2).paid_at)

# повтор долечивает сообщение сотрудникам, если в прошлый раз оно не встало
db.query(Notification).filter_by(order_id=fresh(nr2).id).delete(); db.commit()
paid(nr2, 77000 * 100, charge="tg-retry")
check("R2", "Повтор ставит пропавшее сообщение сотрудникам — ровно одно",
      len(staff_texts(nr2)) == 1)
paid(nr2, 77000 * 100, charge="tg-retry")
check("R2", "Следующий повтор второго сообщения не ставит", len(staff_texts(nr2)) == 1)

# 4. гонки: запись по устаревшему чтению не проходит
r = order("online"); nr4 = r.json()["number"]
other = SessionLocal()
stale = other.query(Order).filter_by(number=nr4).one()      # автоотмена прочитала NEW
paid(nr4, 77000 * 100, charge="tg-race")                     # и тут пришла оплата
moved = order_status.move(other, stale, "CANCELED", unpaid_only=True)
other.commit(); other.close()
check("R4", "Автоотмена по устаревшему чтению не пишет «Отменён» поверх оплаты",
      moved is False and fresh(nr4).status == "CONFIRMED")
paid(nr4, 77000 * 100, charge="tg-race-2")                   # второй платёж по тому же
check("R4", "Второй платёж по оплаченному заказу — «нужен человек», первый не перетёрт",
      fresh(nr4).payment_charge_id == "tg-race"
      and any("уже оплачен" in t for t in staff_texts(nr4)))

# 10. сумма в уведомлении об оплате не сходится
r = order("online"); nr10 = r.json()["number"]
paid(nr10, 1000 * 100, charge="tg-wrong-sum")
check("R10", "Оплачено не столько, сколько стоит заказ — заказ не принят, сотрудникам сигнал",
      fresh(nr10).status == "NEW" and fresh(nr10).paid_at is None
      and any("Нужен возврат" in t for t in staff_texts(nr10)))

# 12. удалить оплаченный заказ нельзя
r = client.delete(f"/api/admin/orders/{fresh(num).id}", headers=ADMIN)
check("R12", "Оплаченный заказ удалить нельзя", r.status_code == 409 and fresh(num) is not None)

# 14. счёт не выставляется заново на каждое нажатие
r = order("online"); nr14 = r.json()["number"]
calls.clear()
for _ in range(3):
    client.post(f"/api/orders/{nr14}/invoice", headers=BUYER)
made = sum(1 for m, _p, _t in calls if m == "createInvoiceLink")
check("R14", "Три нажатия «Оплатить» — один запрос к Telegram", made == 1, f"запросов: {made}")

# 11. текст отказа по заказу, который уже в работе
o11 = fresh(nr2)                                             # оплачен и принят
ans = pre_checkout(nr2, 77000 * 100, qid="q-busy")
check("R11", "Отказ перед оплатой по оплаченному заказу — «уже оплачен», а не «отменён»",
      ans["ok"] is False and "уже оплачен" in ans["error_message"])

# ======================================================= повторное ревью
# A. синхронизация двигает старые онлайн-заказы, законно стоящие у кассы
r = order("online"); na = r.json()["number"]
oa = fresh(na); oa.status = "CONFIRMED"; oa.regos_document_id = 999; db.commit()
check("A", "Старый онлайн-заказ у кассы касса двигает, как раньше («В обработке» → собирается)",
      target_status(fresh(na), {"id": 24, "name": "В обработке"}) == "ASSEMBLING")

# B. неудачная условная запись не врёт в памяти
r = order("online"); nb = r.json()["number"]
s1, s2 = SessionLocal(), SessionLocal()
ob1 = s1.query(Order).filter_by(number=nb).one()
ob2 = s2.query(Order).filter_by(number=nb).one()
order_status.move(s2, ob2, "CANCELED"); s2.commit(); s2.close()
ok_move = order_status.move(s1, ob1, "CONFIRMED")
check("B", "Неудачная запись: объект в памяти показывает то, что в базе",
      ok_move is False and ob1.status == "CANCELED", f"в памяти {ob1.status}")
s1.rollback(); s1.close()

# окно гонки п.4: окно оплаты открылось между чтением и отменой
r = order("online"); nw = r.json()["number"]
ow = fresh(nw); ow.created_at = utcnow() - timedelta(minutes=31); db.commit()
reader = SessionLocal()
seen = reader.query(Order).filter_by(number=nw).one()        # автоотмена прочитала
pre_checkout(nw, 77000 * 100, qid="q-window")                # и тут открылось окно
moved = order_status.move(reader, seen, "CANCELED", unpaid_only=True, where=[
    payments.or_(Order.checkout_at.is_(None),
                 Order.checkout_at <= utcnow() - payments.CHECKOUT_GRACE)])
reader.commit(); reader.close()
check("R4", "Окно оплаты открылось между чтением и отменой — заказ не отменён",
      moved is False and fresh(nw).status == "NEW")

# C. ошибка, которую повтор не лечит, — «ок», а не бесконечные повторы
resp = hook({"update_id": 77, "message": {"chat": {"id": 900}, "from": {"id": 900},
             "successful_payment": "не объект"}})
check("C", "Кривое уведомление об оплате — 200, без бесконечных повторов",
      resp.status_code == 200)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
