# app/web/deps.py
"""라우터 모듈들이 공유하는 것들 (Jinja2 templates, 저장 시 공통으로 쓰는 검증 로직)."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def require_updated_by(updated_by: str) -> str:
    """변경 저장은 입력자명 2글자 이상 필수 (누가 바꿨는지 추적 가능하도록)"""
    name = (updated_by or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="입력자명을 2글자 이상 입력해주세요")
    return name


def resolve_owner(requested: str, updated_by: str, admin_set: set[str]) -> str | None:
    """
    담당자 수정은 관리자만 가능.
    비관리자가 보낸 값이거나 빈 값이면 None을 반환해 기존 담당자를 그대로 유지한다.
    """
    requested = (requested or "").strip()
    if not requested or updated_by not in admin_set:
        return None
    return requested
