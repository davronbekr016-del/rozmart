"""Адрес по точке на карте.

Покупатель ставит булавку на свой дом, а поле адреса заполняется само.
Пишет он потом только то, чего на карте нет: подъезд, этаж, квартиру.

Адреса берём у Nominatim — открытого геокодера OpenStreetMap. Ключа он
не требует, но просит немногого взамен, и это определяет устройство модуля:

* не больше одного запроса в секунду — отсюда общий на всё приложение
  ограничитель, а не «как получится» на каждый запрос браузера;
* запрос должен представляться — отсюда заголовок User-Agent с адресом
  магазина, по которому с нами можно связаться;
* повторные запросы об одном и том же нежелательны — отсюда кэш: пока
  покупатель возит карту туда-сюда, одна и та же точка спрашивается один раз.

Запрос идёт через сервер, а не из браузера: так ограничитель один на всех,
кэш общий, и в Nominatim видно одно приложение, а не сотню разных телефонов.

Если геокодер молчит или отказывает, поле просто остаётся пустым — адрес
покупатель напишет руками, как писал раньше. Заказ от этого не страдает.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Query

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Адреса"])

URL = "https://nominatim.openstreetmap.org/reverse"
TIMEOUT = 8

# Nominatim просит представляться и обещает блокировать тех, кто не считается
# с лимитом. Адрес магазина здесь — способ с нами связаться, если что.
USER_AGENT = "ROZMART shop (https://85.217.171.186.nip.io)"

MIN_INTERVAL = 1.05          # секунды между запросами: у них лимит 1 в секунду
CACHE_LIMIT = 500            # точек в памяти; больше для одного магазина незачем

# Если геокодер отказал (429, 403, недоступен), не долбим его: подождём.
# Молчаливое поле адреса — беда куда меньшая, чем блокировка по IP.
COOLDOWN_SECONDS = 10 * 60

_lock = threading.Lock()
_last_call = 0.0
_blocked_until = 0.0
_cache: dict[tuple[float, float], dict] = {}


def _key(lat: float, lon: float) -> tuple[float, float]:
    """Ключ кэша. Пять знаков — около метра: точки ближе метра друг к другу
    дают один и тот же адрес, спрашивать о каждой отдельно незачем."""
    return (round(lat, 5), round(lon, 5))


def compose(address: dict) -> dict:
    """Разбирает ответ Nominatim на улицу и номер дома.

    display_name брать нельзя: это строка вида «39, Шарк Тонги улица, Бунёдкор
    проспект (дублёр), Чиланзарский район, Ташкент, 100000, Узбекистан» —
    в поле доставки такое не помещается и мешает читать.

    Номер дома отдаём отдельно: в приложении под него своё поле, рядом
    с подъездом и квартирой. Склеивать их обратно в одну строку — работа
    сервера при оформлении заказа (ContactIn.full_address).
    """
    road = (address.get("road") or address.get("pedestrian")
            or address.get("residential") or address.get("amenity"))
    area = (address.get("neighbourhood") or address.get("quarter")
            or address.get("suburb") or address.get("city_district"))
    city = address.get("city") or address.get("town") or address.get("village")

    parts = []
    if road:
        parts.append(road)
    if area and area not in parts:
        parts.append(area)
    # город добавляем, только если ничего конкретнее не нашлось: писать
    # «Ташкент» жителю Ташкента бессмысленно
    if city and not parts:
        parts.append(city)
    return {"address": ", ".join(parts) or None,
            "house": address.get("house_number")}


EMPTY = {"address": None, "house": None}


def lookup(lat: float, lon: float) -> dict:
    """Адрес по координатам. Пустые поля — не нашлось или геокодер недоступен."""
    global _last_call, _blocked_until

    key = _key(lat, lon)
    if key in _cache:
        return _cache[key]

    with _lock:
        if time.time() < _blocked_until:
            return EMPTY
        # ждём свою очередь: лимит общий на всё приложение, поэтому и пауза
        # держится здесь, под замком, а не у каждого запроса своя
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()

        query = urllib.parse.urlencode({
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "format": "jsonv2",
            "accept-language": "ru",
            # 18 — «дом»: без этого приходит район целиком
            "zoom": 18,
        })
        request = urllib.request.Request(
            f"{URL}?{query}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 403):
                _blocked_until = time.time() + COOLDOWN_SECONDS
                log.warning("Геокодер отказал (%s), молчим %s минут",
                            exc.code, COOLDOWN_SECONDS // 60)
            else:
                log.warning("Геокодер ответил %s", exc.code)
            return EMPTY
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("Геокодер недоступен: %s", exc)
            return EMPTY

    found = compose(body.get("address") or {})
    if len(_cache) >= CACHE_LIMIT:
        _cache.clear()          # проще очистить целиком, чем вести очередь
    _cache[key] = found
    return found


@router.get("/geocode")
def geocode(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
):
    """Адрес по точке. Пустой ответ — обычное дело, поле останется за человеком."""
    return lookup(lat, lon)
