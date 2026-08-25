# app/services/eos_report.py
from datetime import date

from app.core.date_utils import week_ranges
from app.core.teams_client import send_teams_message
from app.services.eos import calc_eos_completion, eos_ticket_done_date, filter_track
from app.services.eos_data import get_eos_data

# OS/DB 트랙 전체 대상 수는 프로젝트 착수 시점에 고정된 모수 (엑셀 행 수와 별개 - 지시에 따라 계산하지 않고 고정값 사용)
OS_TOTAL_FIXED = 384
DB_TOTAL_FIXED = 49


def collect_eos(use_jira: bool = True):
    """
    반환: (items, ticket_map, polestar_confirmed)
    수집 자체는 대시보드와 공용(eos_data.get_eos_data) - 두 곳이 따로 구현해 숫자가
    어긋나는 걸 막기 위함. 주간 리포트는 발송 시점의 최신값을 써야 하므로 캐시를 무시한다.
    """
    items, ticket_map, polestar_confirmed, _ = get_eos_data(
        use_external=use_jira, force_refresh=True
    )
    return items, ticket_map, polestar_confirmed


def _month_stats(details: list[dict], today: date) -> tuple[int, int]:
    """이번 달을 목표로 하는 대상 수 / 그 중 완료 수"""
    this_month = [
        d for d in details
        if d["schedule"] and d["schedule"].year == today.year and d["schedule"].month == today.month
    ]
    done = sum(1 for d in this_month if d["completed"])
    return len(this_month), done


def _count_in_window(details: list[dict], ticket_map: dict, start: date, end: date) -> int:
    """이 기간 안에 IP전환 변경계획시작일이 있는 대상 수 (연결된 티켓 기준)"""
    cnt = 0
    for d in details:
        matched = ticket_map.get(d["item_no"]) or []
        if any((dd := eos_ticket_done_date(t)) and start <= dd <= end for t in matched):
            cnt += 1
    return cnt


def _track_section(
    title: str,
    fixed_total: int,
    items: list[dict],
    ticket_map: dict,
    today: date,
    perf_start: date, perf_end: date,
    plan_start: date, plan_end: date,
    polestar_confirmed: set[str] | None = None,
) -> list[str]:
    result = calc_eos_completion(items, ticket_map, today, polestar_confirmed=polestar_confirmed)
    rate = round(result["done"] / fixed_total * 100) if fixed_total else 0

    month_target, month_done = _month_stats(result["details"], today)
    month_rate = round(month_done / month_target * 100) if month_target else 0

    perf_cnt = _count_in_window(result["details"], ticket_map, perf_start, perf_end)
    plan_cnt = _count_in_window(result["details"], ticket_map, plan_start, plan_end)

    return [
        f"[{title}]",
        "2. `26년 하반기 EoS 버전 업그레이드 진행",
        f"   1) 총 {fixed_total}대 中 {result['done']}대 완료 (진행률 {rate}%)",
        "   2) 월별 목표",
        f"       - {today.month}월 목표 {month_target}대 中 {month_done}대 완료 (진행률 {month_rate}%)",
        "   3) 실적",
        f"    - 금주 실적 ({perf_start:%m/%d} ~ {perf_end:%m/%d}) : {perf_cnt}대",
        f"    - 차주 계획 ({plan_start:%m/%d} ~ {plan_end:%m/%d}) : {plan_cnt}대",
        "",
    ]


def build_eos_report(use_jira: bool = True) -> str:
    today = date.today()
    perf_start, perf_end, plan_start, plan_end = week_ranges(today)

    items, ticket_map, polestar_confirmed = collect_eos(use_jira)
    os_items = filter_track(items, "OS")
    db_items = filter_track(items, "DB")

    lines = ["[EoS]", ""]
    lines += _track_section("OS", OS_TOTAL_FIXED, os_items, ticket_map, today, perf_start, perf_end, plan_start, plan_end, polestar_confirmed)
    lines += _track_section("DB", DB_TOTAL_FIXED, db_items, ticket_map, today, perf_start, perf_end, plan_start, plan_end, polestar_confirmed)

    return "\n".join(lines)


def send_eos_report(use_jira: bool = True):
    text = build_eos_report(use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
