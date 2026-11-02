"""DR 상세 목록의 표시용 계산. 완료율과 리포트 계산에는 관여하지 않는다."""
from datetime import date, datetime, timedelta
from typing import Callable, Hashable
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


# 현황 탭(운영팀/관계사/서비스)에 보여줄 미완료 상태. 완료율만으로는 같은 60%라도
# '다음 주 예정'인 그룹과 '날짜가 지났거나 계획이 없는' 그룹이 구분되지 않는다.
PENDING_STATUSES = ("scheduled", "overdue", "approximate", "unplanned")
_STATUS_KEY_BY_LABEL = {label: key for key, label in STATUS_LABELS.items()}


def remind_service_key(row: dict) -> str:
    """리마인드 발송 이력의 서비스 키 (reminder._service_groups와 같은 규칙)"""
    return row.get("business_name") or row.get("system_name") or ""


def status_summary(
    details: list[dict],
    key_fn: Callable[[dict], Hashable],
    today: date,
    last_remind: dict[str, str] | None = None,
    excluded_nos: set[str] | frozenset = frozenset(),
) -> list[dict]:
    """
    그룹별 완료율 + 미완료 상태 건수 + 대표 날짜. 조치가 필요한 순(지연+미계획 많은 순)으로 정렬.

    - next_date      : 예정 중 가장 빠른 일정 (이번에 무엇이 도는지)
    - oldest_overdue : 지연 중 가장 오래된 일정 (독촉이 필요한 곳)
    - last_remind    : 미계획 대상 서비스로 마지막 리마인드를 보낸 시각
                       (미계획은 날짜가 없으니 '요청은 했는지'로 대신 보여준다)
    관리자가 제외(X)한 대상은 완료율 분모에는 그대로 두되(group_by와 같은 기준)
    미완료 상태 건수에는 넣지 않는다 - 일정이 'X'라 '대략'으로 잘못 세진다.
    """
    groups: dict = {}
    for d in details:
        key = key_fn(d)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "key": key, "done": 0, "total": 0, **{s: 0 for s in PENDING_STATUSES},
                "next_date": None, "oldest_overdue": None, "last_remind": "",
            }
        g["total"] += 1
        if d["completed"]:
            g["done"] += 1
            continue
        if d["no"] in excluded_nos:
            continue
        status = _STATUS_KEY_BY_LABEL.get(d["status_label"])
        if status not in PENDING_STATUSES:
            continue
        g[status] += 1
        sched = d.get("schedule")
        if status == "scheduled" and sched and (g["next_date"] is None or sched < g["next_date"]):
            g["next_date"] = sched
        elif status == "overdue" and sched and (g["oldest_overdue"] is None or sched < g["oldest_overdue"]):
            g["oldest_overdue"] = sched
        elif status == "unplanned":
            sent = (last_remind or {}).get(remind_service_key(d), "")
            if sent > g["last_remind"]:
                g["last_remind"] = sent

    for g in groups.values():
        g["rate"] = round(g["done"] / g["total"] * 100, 1) if g["total"] else 0.0
        g["action"] = g["overdue"] + g["unplanned"]
        g["next_in_days"] = (g["next_date"] - today).days if g["next_date"] else None
        g["overdue_days"] = (today - g["oldest_overdue"]).days if g["oldest_overdue"] else None
    return sorted(groups.values(), key=lambda g: (-g["action"], g["rate"], str(g["key"])))
