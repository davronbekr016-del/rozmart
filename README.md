# ROZMART Mini App

Telegram Mini App для сети магазинов ROZMART: каталог продукции ROZMETOV, заказ, доставка.

Требования и процесс описаны в папке `Загрузки\Проект ROZMART` — SRS, Регламент, BPMN-схема, прототипы.

## Карта функционала

Что уже готово — смотреть сюда до написания нового кода.

| Модуль | Что умеет | Неочевидное |
|---|---|---|
| `app/db.py` | Подключение к базе, `get_db`, `init_db` | Для SQLite включает `foreign_keys`, `WAL` и `busy_timeout` — без них запись заказа блокирует чтение каталога |
| `app/models.py` | `Category` → `Product` → `Variant`, `Order` → `OrderItem` | `Variant.price` может быть `NULL` (прайса ещё нет), но не `0`. `Product.is_active` по умолчанию `false` (BR-34). `Order.client_key` — ключ попытки оформления |
| `app/schemas.py` | Что API отдаёт клиенту и что принимает | Служебных полей REGOS здесь нет намеренно (BR-36). Телефон нормализуется к `+998…` |
| `app/catalog.py` | `GET /api/categories`, `/api/products`, `/api/products/{id}` | Товар виден, только если активны и он, и хотя бы одна фасовка с ценой. Цена «от» — по видимым фасовкам (BR-37) |
| `app/orders.py` | `POST /api/orders`, `GET /api/orders/{number}`, `GET /api/delivery-price` | Суммы считает сервер по базе, тело запроса на цену не влияет. Повтор с тем же `client_key` возвращает прежний заказ. `expected_total` не сошёлся → 409. Наличные → `CONFIRMED`, онлайн → `NEW` (BR-13) |
| `scripts/import_catalog.py` | Заливает 55 позиций и фото из Excel | Товар опознаётся по коду REGOS, а не по названию. Коды, пропавшие из файла, скрываются. Фото называются `{product_id}.jpg`, чтобы код REGOS не утёк в URL |
| `static/app.js` | Каталог, карточка, корзина, оформление | Корзина лежит в `localStorage` (`rozmart.cart.v1`) и сверяется с сервером при открытии. Максимум 99 штук одной позиции — столько же принимает сервер |

Ещё не сделано: авторизация через Telegram, оплата, админка, интеграция с REGOS.

## Запуск

```
.venv\Scripts\activate
uvicorn app.main:app --reload
```

Проверка: http://127.0.0.1:8000/health

## Установка с нуля

```
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## База данных

Разработка идёт на SQLite — файл `rozmart.db` создаётся автоматически при запуске.
Для другой базы задайте переменную окружения `DATABASE_URL`, код менять не нужно:

```
set DATABASE_URL=postgresql+psycopg://user:pass@host/rozmart
```

**Важно:** миграций пока нет. После изменения моделей в `app/models.py` удалите
`rozmart.db` и запустите сервер заново — иначе новые колонки не появятся,
а ошибка вылезет позже и в неочевидном месте.

Каталог после этого заливается заново:

```
.venv\Scripts\python.exe scripts\import_catalog.py
```
