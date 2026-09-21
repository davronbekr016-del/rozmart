"""Фотографии товаров из REGOS.

В REGOS у номенклатуры бывает картинка — метод `ItemImage/Get` отдаёт ссылку
на неё в их CDN. Из 8050 позиций каталога такие нашлись у 167, и почти все
они — про наш ассортимент: из опубликованных фасовок картинка есть у 43 из 49.

Запускается кнопкой в панели, а не при синхронизации каталога. Причина простая:
это десятки скачиваний по несколько сотен килобайт, и делать их при каждом
регламентном проходе (раз в полчаса) незачем — картинки в учётной системе
меняются раз в год.

Уже загруженные вручную фотографии не трогаются никогда. Свои сняты и обрезаны
под витрину, а в REGOS лежит то, что удобно кассиру; затирать одно другим
нельзя — по той же причине, по которой синхронизация не трогает витринные
названия и описания.
"""
import logging
import urllib.error
import urllib.request

from sqlalchemy import select

from app import photos
from app.models import Product, Variant
from app.regos.client import RegosClient

log = logging.getLogger(__name__)

PAGE = 500
TIMEOUT = 30

# Картинка товара — это фотография, а не фотоальбом: четыре мегабайта столько
# же, сколько принимает ручная загрузка в панели.
MAX_BYTES = 4 * 1024 * 1024

# Что умеем показывать. REGOS отдаёт png и jpg; всё прочее пропускаем,
# а не сохраняем с неверным расширением.
TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def fetch_images(client: RegosClient) -> dict[int, str]:
    """id номенклатуры -> ссылка на картинку."""
    found: dict[int, str] = {}
    offset = 0
    while True:
        result = client.call("ItemImage/Get", {"limit": PAGE, "offset": offset})
        rows = result.get("result") if isinstance(result, dict) else result
        rows = rows or []
        for row in rows:
            if row.get("url") and row.get("item_id"):
                # у позиции бывает несколько снимков: берём первый
                found.setdefault(row["item_id"], row["url"])
        if len(rows) < PAGE:
            return found
        offset += PAGE


def fetch_codes(client: RegosClient, item_ids: list[int]) -> dict[int, str]:
    """id номенклатуры -> её код, в том же виде, что хранится у фасовок.

    Картинки приходят с внутренним id, а витрина знает товар по коду
    (`Variant.external_code`), поэтому одно к другому надо привести.
    Фильтр `id In` принимает список строкой через запятую — массивом
    тот же метод отвечает ошибкой 1008.
    """
    codes: dict[int, str] = {}
    for start in range(0, len(item_ids), 100):
        chunk = item_ids[start:start + 100]
        result = client.call("Item/Get", {
            "filters": [{"Field": "id", "Operator": "In",
                         "Value": ",".join(str(i) for i in chunk)}],
            "limit": len(chunk),
            "offset": 0,
        })
        rows = result.get("result") if isinstance(result, dict) else result
        for row in rows or []:
            codes[row["id"]] = f"{int(row['code']):06d}"
    return codes


def download(url: str, target_stem: str) -> str | None:
    """Скачивает картинку. Возвращает имя файла или None, если не вышло."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            suffix = TYPES.get((response.headers.get("Content-Type") or "").split(";")[0])
            if suffix is None:
                log.info("Пропускаю %s: тип %s", url, response.headers.get("Content-Type"))
                return None
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Не скачалась картинка %s: %s", url, exc)
        return None

    if len(data) > MAX_BYTES:
        log.info("Пропускаю %s: больше %s МБ", url, MAX_BYTES // 1024 // 1024)
        return None

    photos.DIR.mkdir(parents=True, exist_ok=True)
    name = f"{target_stem}{suffix}"
    (photos.DIR / name).write_bytes(data)
    return name


def candidates(db, codes: dict[str, str]) -> list[tuple[Product, str]]:
    """Карточки без фотографии, для которых в REGOS картинка есть."""
    rows: list[tuple[Product, str]] = []
    for product in db.scalars(select(Product).where(Product.photo.is_(None))).unique():
        for variant in product.variants:
            url = codes.get(variant.external_code)
            if url:
                rows.append((product, url))
                break
    return rows


def run(db, client: RegosClient | None = None, apply: bool = True) -> dict:
    """Подтягивает картинки. apply=False — только посчитать, ничего не скачивая."""
    client = client or RegosClient()
    images = fetch_images(client)
    codes_by_id = fetch_codes(client, sorted(images))
    by_code = {code: images[item_id] for item_id, code in codes_by_id.items()}

    waiting = candidates(db, by_code)
    if not apply:
        return {"in_regos": len(images), "candidates": len(waiting),
                "loaded": 0, "failed": 0, "applied": False}

    loaded, failed = 0, 0
    for product, url in waiting:
        # имя файла — по id карточки: код REGOS в публичный адрес не попадает (BR-36)
        name = download(url, str(product.id))
        if name is None:
            failed += 1
            continue
        product.photo = name
        loaded += 1
    if loaded:
        db.commit()
    log.info("Фотографии из REGOS: загружено %s, не вышло %s", loaded, failed)
    return {"in_regos": len(images), "candidates": len(waiting),
            "loaded": loaded, "failed": failed, "applied": True}
