from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.catalog import router as catalog_router
from app.db import init_db
from app.orders import router as orders_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    """Клиентское приложение."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}
