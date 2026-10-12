"""DR 상세 목록의 표시용 계산. 완료율과 리포트 계산에는 관여하지 않는다."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.change_history import annotate_recent_changes

ACTION_LABELS = {
    "all": "전체",
    "evidence": "증적 미입력",
    "schedule": "일정 미등록",
    "overdue": "예정일 경과",
    "warning": "증적 확인 필요",
    "recent": "최근 7일 변경",
}

STATUS_LABELS = {
    "pending": "미완료", "scheduled": "예정", "overdue": "지연",
    "approximate": "대략", "unplanned": "미계획", "done": "완료",
}


def matches_status(row: dict, status: str) -> bool:
    if status == "done":
        return bool(row["completed"])
    if status == "pending":
        return not row["completed"]
    return row["status_label"] == STATUS_LABELS[status]


def select_details(details: list[dict], status: str | None, action: str):
    """필터 적용 전 대수를 계산하고 상태/조치 중 하나만 적용한다. 원본은 변경하지 않는다."""
    counts = action_counts(details)
    status_counts = {key: sum(matches_status(d, key) for d in details) for key in STATUS_LABELS}
    if action not in ACTION_LABELS:
        action = "all"
    if action == "overdue":
        status, action = "overdue", "all"
    if action != "all":
        status = None
        selected = [d for d in details if d["actions"][action]]
    elif status in STATUS_LABELS:
        selected = [d for d in details if matches_status(d, status)]
    else:
        status = None
        selected = list(details)
    return selected, status, action, counts, status_counts


def recently_changed(value: str | None, now: datetime) -> bool:
    if value is None:
        return False
    try:
        changed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    # SQLite의 로컬 시각은 애플리케이션 시간대로 해석한다.
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=now.tzinfo)
    return now - timedelta(days=7) <= changed <= now


def decorate_details(details: list[dict], timezone: str, half: str | None = None) -> None:
    """
    행별 표시용 필드(recent_change/actions)를 붙인다.

    half가 주어지면 감사 로그(change_log) 기반으로 누가/언제/무엇을 바꿠는지까지
    담은 상세 요약(dict)을 붙인다 (change_history.annotate_recent_changes 참고).
    half가 없으면(기존 호출부/단위 테스트 호환) updated_at만으로 단순 bool 판정으로 폴백한다.
    """
    now = datetime.now(ZoneInfo(timezone))
    if half:
        annotate_recent_changes(details, half, now=now.replace(tzinfo=None))
    for row in details:
        if half:
            recent = bool(row.get("recent_change"))
        else:
            recent = recently_changed(row.get("updated_at", ""), now)
            row["recent_change"] = recent
        row["actions"] = {
            "evidence": not bool((row.get("evidence") or "").strip()),
            "schedule": not row.get("completed") and not bool((row.get("schedule_raw") or "").strip()),
            "overdue": row.get("status_label") == "지연",
            "warning": bool(row.get("evidence_level")),
            "recent": recent,
        }


def action_counts(details: list[dict]) -> dict[str, int]:
    return {key: len(details) if key == "all" else sum(bool(d["actions"][key]) for d in details)
            for key in ACTION_LABELS}
