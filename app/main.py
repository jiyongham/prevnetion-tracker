# app/main.py
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.scheduler import start_scheduler, stop_scheduler
from app.models.db import init_db
from app.web.deps import WEB_DIR
from app.web.routes import capacity, dr, misc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    init_db()
    logger.info("✅ DB 초기화 완료")
    start_scheduler()
    yield
    # shutdown
    stop_scheduler()


app = FastAPI(title="장애예방 활동 진척 관리", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(WEB_DIR / "static")),
    name="static",
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

app.include_router(dr.router)
app.include_router(capacity.router)
app.include_router(misc.router)
