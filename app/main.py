# app/main.py
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.scheduler import start_scheduler, stop_scheduler
from app.models.db import init_db
from app.services.dr_data import prewarm as prewarm_dr
from app.services.eos_data import prewarm as prewarm_eos
from app.services.report import get_current_half
from app.web.deps import WEB_DIR
from app.web.routes import capacity, dr, eos, home, misc

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
    # 외부 조회(JIRA/Polestar)가 무거워 기동 직후 백그라운드로 캐시를 채운다.
    # DR훈련은 지금 반기만 - 지난 반기 화면은 열어보는 사람이 있을 때 채워도 늦지 않다.
    prewarm_eos()
    prewarm_dr(get_current_half())
    yield
    # shutdown
    stop_scheduler()


app = FastAPI(title="장애예방 관제센터", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(WEB_DIR / "static")),
    name="static",
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

app.include_router(home.router)
app.include_router(dr.router)
app.include_router(capacity.router)
app.include_router(eos.router)
app.include_router(misc.router)