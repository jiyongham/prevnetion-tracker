# app/services/change_history.py
"""
최근 변경 하이라이트 (대시보드 "최근 7일 변경" 표시용).

change_log(app.models.db)를 반기 단위로 모아 각 행(item_no)에 "최근에 뭐가
바뀌었는지"를 붙여준다. 완료/제외 판정 로직(completion.py)은 건드리지 않는다 -
여긴 순전히 화면 표시용 부가 정보다.

관리자 이름 마스킹: 감사 로그(change_log.updated_by)에는 실제 관리자 이름이 그대로
남아야 한다 (누가 왜 바꿨는지 나중에 추적해야 하므로) - 여기서 하는 일은 그 값을
읽어 "화면에 보여줄 라벨"만 가공하는 것이고, DB에 쓰는 것도 아니고 원본 로그를
지우거나 바꾸지도 않는다. 제외 판정(completion.excluded_completion_date)이 보는
updated_by 원본도 이 모듈을 거치지 않으므로 영향이 없다.
"""
import logging
from datetime import datetime, timedelta

from app.config import settings
from app.core.date_utils import parse_db_datetime
from app.models.db import get_recent_logs_by_half

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "schedule": "일정",
    "mode": "수행방식",
    "is_done": "완료",
    "evidence": "증적",
    "note": "비고",
    "owner": "담당자",
    "exclude_reason": "제외사유",
}

ADMIN_DISPLAY_LABEL = "관리자"


def mask_changer(name: str) -> str:
    """
    변경자 이름을 화면에 보여줄 라벨로. 관리자는 실명 대신 '관리자'로 가려서 보여준다
    (감사 로그 원본의 updated_by는 그대로 두고 표시만 가린다 - 모듈 docstring 참고).
    """
    name = (name or "").strip()
    if not name:
        return ""
    return ADMIN_DISPLAY_LABEL if name in settings.admin_set else name


def _field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def _summarize_logs(logs: list[dict]) -> dict | None:
    """
    같은 item_no의 로그 묶음(최근순) -> 화면용 요약 하나.
    가장 최근 1건의 시각/변경자를 대표로 쓰고, 그 시각 묶음(한 번의 저장으로 여러
    필드가 같이 바뀐 경우)의 필드명을 모아 보여준다. 로그 자체는 필드별로 한 행씩
    나뉘어 들어오므로(upsert_input 참고), 가장 최근 updated_at과 같은 시각의
    항목들을 '한 번의 저장'으로 묶는다.
    """
    if not logs:
        return None

    latest_at_raw = logs[0]["updated_at"]
    latest_dt = parse_db_datetime(latest_at_raw)
    if latest_dt is None:
        # 형식이 깨진 타임스탬프는 신뢰할 수 없으므로 하이라이트 대상에서 제외한다
        # (잘못된 날짜로 "방금 바뀜"처럼 보이게 하지 않기 위함).
        return None

    same_batch = [l for l in logs if l["updated_at"] == latest_at_raw]
    fields = sorted({_field_label(l["field"]) for l in same_batch})
    changer_raw = logs[0].get("updated_by") or ""

    return {
        "at": latest_at_raw,
        "at_dt": latest_dt,
        "by_raw": changer_raw,            # 내부용(감사 추적) - 화면엔 쓰지 않는다
        "by_display": mask_changer(changer_raw),
        "fields": fields,
        "summary": f"{', '.join(fields)} 변경" if fields else "변경",
    }


def annotate_recent_changes(
    details: list[dict], half: str, now: datetime | None = None, window_days: int = 7
) -> list[dict]:
    """
    각 행에 recent_change(dict|None)를 붙인다 (원본 dict를 그대로 수정).

    recent_change가 있으면: {"at", "by_display", "summary", "fields"} - 템플릿은
    이 중 by_display/at/summary만 노출한다 (by_raw는 내부용).

    window_days 안에 변경 이력이 없거나, 타임스탬프가 깨져 신뢰할 수 없으면 None.
    DB 조회 실패(예: 테이블 없음)는 전체 화면을 막으면 안 되므로 조용히 생략한다.
    """
    now = now or datetime.now()
    since_dt = now - timedelta(days=window_days)
    since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        logs_by_no = get_recent_logs_by_half(half, since_str)
    except Exception as e:
        logger.warning(f"최근 변경 이력 조회 실패 (하이라이트 생략): {e}")
        return details

    for d in details:
        logs = logs_by_no.get(d["no"])
        if not logs:
            d["recent_change"] = None
            continue
        summary = _summarize_logs(logs)
        # 파싱된 시각이 미래거나 window 밖이면(시계 오차 등 이상치) 표시하지 않는다
        if summary and since_dt <= summary["at_dt"] <= now + timedelta(minutes=5):
            d["recent_change"] = summary
        else:
            d["recent_change"] = None

    return details
