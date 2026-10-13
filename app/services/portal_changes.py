"""포털 홈의 '최근 변경' 통합 조회 (DR/용량관리/EoS, 커널패치 제외)."""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.db import (
    get_recent_logs_by_half,
    get_recent_capacity_logs,
    get_recent_eos_logs,
)
from app.services.change_history import FIELD_LABELS, mask_changer

logger = logging.getLogger(__name__)


def get_portal_recent_changes(
    now: datetime | None = None, window_days: int = 7, limit: int = 20
) -> list[dict]:
    """
    DR/용량관리/EoS의 최근 변경을 병합해 시간순(내림차순)으로 반환.
    
    Returns:
        각 항목은 {
            "domain": "DR"|"용량관리"|"EoS",
            "domain_href": "/dr"|"/capacity"|"/eos",
            "item_no": str,
            "field_label": str (한국어),
            "updated_by_display": str,
            "updated_at": str (ISO 형식),
        }
    """
    now = now or datetime.now()
    since_dt = now - timedelta(days=window_days)
    since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    all_logs: list[dict] = []
    
    # DR: 두 반기 모두 조회
    try:
        for half in ("H1", "H2"):
            dr_logs_by_item = get_recent_logs_by_half(half, since_str, limit=limit * 10)
            for item_no, logs in dr_logs_by_item.items():
                for log in logs:
                    all_logs.append({
                        "domain": "DR",
                        "domain_href": "/dr",
                        "item_no": item_no,
                        "field": log.get("field", ""),
                        "updated_by": log.get("updated_by", ""),
                        "updated_at": log.get("updated_at", ""),
                    })
    except Exception as e:
        logger.warning(f"DR 최근 변경 조회 실패: {e}")
    
    # 용량관리
    try:
        cap_logs = get_recent_capacity_logs(since_str, limit=limit * 10)
        for log in cap_logs:
            all_logs.append({
                "domain": "용량관리",
                "domain_href": "/capacity",
                "item_no": log.get("item_no", ""),
                "field": log.get("field", ""),
                "updated_by": log.get("updated_by", ""),
                "updated_at": log.get("updated_at", ""),
            })
    except Exception as e:
        logger.warning(f"용량관리 최근 변경 조회 실패: {e}")
    
    # EoS
    try:
        eos_logs = get_recent_eos_logs(since_str, limit=limit * 10)
        for log in eos_logs:
            all_logs.append({
                "domain": "EoS",
                "domain_href": "/eos",
                "item_no": log.get("item_no", ""),
                "field": log.get("field", ""),
                "updated_by": log.get("updated_by", ""),
                "updated_at": log.get("updated_at", ""),
            })
    except Exception as e:
        logger.warning(f"EoS 최근 변경 조회 실패: {e}")
    
    # 시간순 정렬 (내림차순)
    try:
        all_logs.sort(key=lambda x: x["updated_at"], reverse=True)
    except Exception:
        pass
    
    # 필드 라벨 번역 및 관리자 마스킹
    result = []
    for log in all_logs[:limit]:
        field_label = FIELD_LABELS.get(log["field"], log["field"])
        result.append({
            "domain": log["domain"],
            "domain_href": log["domain_href"],
            "item_no": log["item_no"],
            "field_label": field_label,
            "updated_by_display": mask_changer(log["updated_by"]),
            "updated_at": log["updated_at"],
        })
    
    return result
