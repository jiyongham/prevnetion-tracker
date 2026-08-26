# app/services/eos_report.py
from datetime import date

from app.config import settings
from app.core.date_utils import week_ranges
from app.core.eos_loader import get_targets
from app.core.teams_client import send_teams_message
from app.models.db import get_eos_next_week_plan
from app.services.eos import calc_eos_completion, filter_track
from app.services.eos_confluence import get_week_plan_count
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


def _track_section(
    title: str,
    fixed_total: int,
    items: list[dict],
    ticket_map: dict,
    today: date,
    perf_start: date, perf_end: date,
    plan_start: date, plan_end: date,
    polestar_confirmed: set[str] | None = None,
    perf_matched: dict | None = None,
    plan_saved: dict | None = None,
) -> list[str]:
    result = calc_eos_completion(items, ticket_map, today, polestar_confirmed=polestar_confirmed)
    rate = round(result["done"] / fixed_total * 100) if fixed_total else 0

    month_target, month_done = _month_stats(result["details"], today)
    month_rate = round(month_done / month_target * 100) if month_target else 0

    # perf_matched/plan_saved는 전체 대상 기준으로 한 번만 조회된 것 - 이 트랙(OS/DB)
    # 소속 item_no로만 걸러서 센다.
    track_item_nos = {d["item_no"] for d in result["details"]}
    perf_cnt = len(track_item_nos & (perf_matched or {}).keys())
    plan_cnt = len(track_item_nos & (plan_saved or {}).keys())

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
    all_targets = get_targets(items)

    # 금주 실적(발송 기준 차주): JIRA에 티켓이 아직 안 잡힌 작업도 Confluence 주간계획
    # 페이지엔 미리 올라오는 경우가 많아, JIRA 대신 Confluence 파싱 기준으로 센다.
    perf_matched: dict = {}
    try:
        perf_result = get_week_plan_count(
            settings.confluence_eos_parent_page_id, perf_start, perf_end, all_targets
        )
        perf_matched = perf_result["matched"]
    except Exception as e:
        print(f"⚠️ EoS Confluence 조회 실패 (금주 실적 0으로 표시): {e}")

    # 차주 계획(발송 기준 차차주): 아직 취합 자체가 어려운 시점이라, 관리자가 챗봇
    # (/eos/plan-chat)으로 확인해서 수동 입력해둔 값을 쓴다.
    plan_saved = get_eos_next_week_plan(plan_start.isoformat())

    lines = ["[EoS]", ""]
    lines += _track_section(
        "OS", OS_TOTAL_FIXED, os_items, ticket_map, today, perf_start, perf_end,
        plan_start, plan_end, polestar_confirmed, perf_matched, plan_saved,
    )
    lines += _track_section(
        "DB", DB_TOTAL_FIXED, db_items, ticket_map, today, perf_start, perf_end,
        plan_start, plan_end, polestar_confirmed, perf_matched, plan_saved,
    )

    return "\n".join(lines)


def send_eos_report(use_jira: bool = True):
    text = build_eos_report(use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
