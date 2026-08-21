# app/services/eos_report.py
from datetime import date

from app.config import settings
from app.core.date_utils import week_ranges
from app.core.eos_loader import load_eos_items_merged
from app.core.jira_client import jira
from app.core.teams_client import send_teams_message
from app.services.eos import build_eos_ticket_summary, calc_eos_completion, eos_ticket_done_date
from app.services.matcher import match_items_by_ip

# OS/DB 트랙 전체 대상 수는 프로젝트 착수 시점에 고정된 모수 (엑셀 행 수와 별개 - 지시에 따라 계산하지 않고 고정값 사용)
OS_TOTAL_FIXED = 384
DB_TOTAL_FIXED = 49


def collect_eos(use_jira: bool = True):
    items = load_eos_items_merged()
    ticket_map = {}
    if use_jira:
        try:
            issues = jira.get_eos_tickets()
            tickets = build_eos_ticket_summary(issues, settings.planned_end_date_field)
            targets = [i for i in items if i["is_target"]]
            match_result = match_items_by_ip(targets, tickets)
            ticket_map = match_result["matched"]
        except Exception as e:
            print(f"⚠️ EoS JIRA 조회 실패 (엑셀 기준으로 계속): {e}")
    return items, ticket_map


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
) -> list[str]:
    result = calc_eos_completion(items, ticket_map, today)
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


def _track_items(items: list[dict], track_target_field: str) -> list[dict]:
    """
    '제품별 EoS 일정' 표로 판별한 os_eos_target/db_eos_target 기준으로 트랙별 대상만 골라낸다.
    같은 target이어도 OS만 EoS 대상이고 DB는 아직 아닌 경우가 있어(반대도 마찬가지),
    calc_eos_completion이 보는 is_target을 트랙 기준으로 다시 씌운 사본을 만든다.
    """
    result = []
    for i in items:
        i2 = dict(i)
        i2["is_target"] = bool(i["is_target"] and i.get(track_target_field))
        result.append(i2)
    return result


def build_eos_report(use_jira: bool = True) -> str:
    today = date.today()
    perf_start, perf_end, plan_start, plan_end = week_ranges(today)

    items, ticket_map = collect_eos(use_jira)
    os_items = _track_items(items, "os_eos_target")
    db_items = _track_items(items, "db_eos_target")

    lines = ["하반기", ""]
    lines += _track_section("OS", OS_TOTAL_FIXED, os_items, ticket_map, today, perf_start, perf_end, plan_start, plan_end)
    lines += _track_section("DB", DB_TOTAL_FIXED, db_items, ticket_map, today, perf_start, perf_end, plan_start, plan_end)

    return "\n".join(lines)


def send_eos_report(use_jira: bool = True):
    text = build_eos_report(use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
