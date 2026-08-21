# app/web/routes/misc.py
"""앱 전역 라우트 (헬스체크, 스케줄러 상태)"""
from fastapi import APIRouter

from app.core.scheduler import get_jobs_info

router = APIRouter()


@router.get("/api/jobs")
def api_jobs():
    return {"jobs": get_jobs_info()}


@router.get("/health")
def health():
    return {"status": "ok"}
