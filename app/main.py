from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.catalog import router as catalog_router
from app.db import init_db
from app.orders import router as orders_router
from app.telegram import check_configuration

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_configuration()
    # папки может не быть при установке с нуля: фотографии в репозиторий не попадают
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title="ROZMART Mini App", version="0.1.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

app.include_router(catalog_router)
app.include_router(orders_router)


@app.get("/", include_in_schema=False)
def index():
    """Клиентское приложение.

    Браузер внутри Telegram держит скрипт в кэше и после обновления показывает
    старую версию. Поэтому саму страницу запрещаем кэшировать, а к скрипту
    дописываем время его правки: у новой версии другой адрес, и кэш промахнётся.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    version = int((STATIC_DIR / "app.js").stat().st_mtime)
    return HTMLResponse(
        html.replace("__V__", str(version)),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
