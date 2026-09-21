"""Клиент REGOS API.

Три особенности API, из-за которых обычный requests-вызов работает неправильно:

1. Ответ всегда HTTP 200. Бизнес-ошибка лежит в теле: {"ok": false, "result":
   {"error": 1029, "description": "..."}}. Судить об успехе по коду состояния
   нельзя — 4xx/5xx приходят только от инфраструктуры, до API запрос не дошёл.

2. Лимит 2 запроса в секунду при накопительной ёмкости 50. Исчерпали — приходит
   ok:false с кодом 8213, снова под видом HTTP 200. Поэтому троттлинг встроен
   в клиент, а не оставлен на совесть вызывающего кода.

3. На последней странице next_offset возвращается 0, а не повтор предыдущего
   значения. Цикл вида offset = next_offset из-за этого уходит на второй круг
   и крутится бесконечно. Позиция считается самостоятельно, стоп — по total.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from app.regos import config

log = logging.getLogger(__name__)

TIMEOUT = 40
RATE = 2.0        # запросов в секунду, разрешённых REGOS
BURST = 50.0      # сколько можно накопить про запас

RATE_LIMIT_ERROR = 8213
RETRIABLE_HTTP = {429, 500, 502, 503, 504}


class RegosError(Exception):
    """Бизнес-ошибка REGOS: ok=false. Код и описание пришли от API."""

    def __init__(self, code: int | None, description: str, method: str):
        super().__init__(f"{method}: REGOS error {code}: {description}")
        self.code = code
        self.description = description
        self.method = method


class RegosClient:
    def __init__(self, endpoint: str | None = None, *, rate: float = RATE):
        self.endpoint = (endpoint or config.endpoint()).rstrip("/")
        self._rate = rate
        self._tokens = BURST
        self._last = time.monotonic()

    # ---------- транспорт ----------

    def _throttle(self) -> None:
        now = time.monotonic()
        self._tokens = min(BURST, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens < 1.0:
            time.sleep((1.0 - self._tokens) / self._rate)
            self._tokens = 0.0
            self._last = time.monotonic()
        else:
            self._tokens -= 1.0

    def call(self, method: str, params: dict | None = None, *, retries: int = 4) -> Any:
        """Вызов метода вида 'Item/GetExt'. Возвращает содержимое result.

        Ключ интеграции сидит в URL, поэтому заголовок авторизации не нужен —
        это локальная интеграция, а не тиражируемая с OAuth.
        """
        url = f"{self.endpoint}/{method}"
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        delay = 1.0

        for attempt in range(1, retries + 1):
            self._throttle()
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json;charset=utf-8",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    raw, status = response.read().decode("utf-8", "replace"), response.status
            except urllib.error.HTTPError as exc:
                raw, status = exc.read().decode("utf-8", "replace"), exc.code
            except urllib.error.URLError as exc:
                if attempt == retries:
                    raise
                log.warning("REGOS %s: сеть недоступна (%s), повтор через %.0fs",
                            method, exc.reason, delay)
                time.sleep(delay)
                delay *= 2
                continue

            if status != 200:
                if status in RETRIABLE_HTTP and attempt < retries:
                    log.warning("REGOS %s: HTTP %s от инфраструктуры, повтор через %.0fs",
                                method, status, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"REGOS {method}: HTTP {status}: {raw[:300]}")

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"REGOS {method}: ответ не JSON: {raw[:300]}") from exc

            if data.get("ok"):
                return data.get("result")

            error = data.get("result") or {}
            code = error.get("error")
            if code == RATE_LIMIT_ERROR and attempt < retries:
                log.warning("REGOS %s: лимит запросов, повтор через %.0fs", method, delay)
                time.sleep(delay)
                delay *= 2
                continue

            raise RegosError(code, error.get("description", ""), method)

        raise RuntimeError(f"REGOS {method}: исчерпаны попытки")

    # ---------- выборки ----------

    def paginate(self, method: str, params: dict | None = None, *, page: int = 500):
        """Постраничный обход выборки. Отдаёт записи по одной.

        Опирается на total, а не на next_offset: на последней странице REGOS
        возвращает next_offset = 0, и доверчивый цикл зациклился бы.
        """
        params = dict(params or {})
        offset = 0
        while True:
            params.update({"limit": page, "offset": offset})
            result = self.call(method, params)

            # Часть методов отдаёт голый список, часть — объект с result/total.
            if isinstance(result, dict):
                rows, total = result.get("result") or [], result.get("total")
            else:
                rows, total = result or [], None

            if not rows:
                return
            yield from rows

            offset += len(rows)
            if total is not None and offset >= total:
                return
            if total is None and len(rows) < page:
                return


def account_info(client: RegosClient) -> dict:
    """Тариф и статус аккаунта. Самый дешёвый способ проверить, что ключ жив
    и что включены нужные опции (api_access, webhook_able)."""
    return client.call("sys/getinfo")
