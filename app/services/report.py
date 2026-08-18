# app/services/report.py
from datetime import date, timedelta

from app.config import settings
from app.core.jira_client import jira
from app.core.excel_loader import get_targets, load_dr_items, scope_h2_targets
from app.core.teams_client import send_teams_message
from app.services.completion import (
    build_ticket_summary,
    calc_completion,
    ticket_done_date,
)
from app.services.matcher import match_items_by_ip


def get_current_half() -> str:
    """현재 반기 자동 판별"""
    return "H1" if date.today().month <= 6 else "H2"


def collect(half: str, use_jira: bool = True):
    items = load_dr_items(half=half)
    ticket_map = {}

    if use_jira:
        try:
            issues = jira.get_dr_tickets()
            tickets = build_ticket_summary(issues, settings.planned_end_date_field)
            targets = [i for i in items if i["is_target"]]
            match_result = match_items_by_ip(targets, tickets)
            ticket_map = match_result["matched"]
        except Exception as e:
            print(f"⚠️ JIRA 조회 실패 (엑셀 기준으로 계속): {e}")

    return items, ticket_map


def _fmt_rate(rate: float) -> str:
    """100.0 -> '100', 5.0 -> '5', 4.5 -> '4.5'"""
    return f"{rate:g}"


def _week_ranges(today: date):
    """
    발송일(목요일) 기준 주간 구간
    - 금주 실적: 발송 다음 주 (월~금)
    - 차주 계획: 그 다음 주 (월~금)
    """
    this_monday = today - timedelta(days=today.weekday())
    perf_start = this_monday + timedelta(days=7)      # 금주 실적 (월)
    perf_end = perf_start + timedelta(days=4)          # (금)
    plan_start = perf_start + timedelta(days=7)        # 차주 계획 (월)
    plan_end = plan_start + timedelta(days=4)          # (금)
    return perf_start, perf_end, plan_start, plan_end


# 제외 대상 사유 (고정) — 이후 폐기분은 제외가 아니라 완료로 처리
EXCLUDED_REASON = "미사용 VM 폐기 예정"


def build_report(half: str | None = None, use_jira: bool = True) -> str:
    today = date.today()
    year2 = str(today.year)[2:]  # 2026 -> "26"

    h2_items, h2_tmap = collect("H2", use_jira)

    # 하반기 대상 = 상반기 무중단으로 수행한 대상에 한함
    h2_scope = scope_h2_targets(h2_items)
    h2_result = calc_completion(h2_scope, h2_tmap, today)

    # ── 1. 전체 대상 (제외 고정) ──
    total_all = len(h2_items)
    target_cnt = len(get_targets(h2_items))
    excluded = total_all - target_cnt

    lines = [
        "[DR훈련]",
        "1. 전체 대상",
        f"- 총 {total_all}대 中 {target_cnt}대, 제외 {excluded}대",
        f"    ※ {EXCLUDED_REASON} (대상 제외 {excluded}대)",
        "",
    ]

    # ── 2. 하반기 (상반기 무중단 대상 / 주간 실적·계획) ──
    perf_start, perf_end, plan_start, plan_end = _week_ranges(today)

    # 금주 실적: 다음 주에 완료일(실전환=변경계획완료일 / 무중단=생성일)이 있는 대상 수
    def _has_ticket_in(nos_tickets, start, end):
        return any(
            (d := ticket_done_date(t)) and start <= d <= end
            for t in (nos_tickets or [])
        )

    perf_cnt = sum(
        1 for item in get_targets(h2_scope)
        if _has_ticket_in(h2_tmap.get(item["no"]), perf_start, perf_end)
    )

    # 차주 계획: 그 다음 주에 웹/엑셀 등록 일정이 잡힌 대수
    plan_cnt = sum(
        1 for d in h2_result["details"]
        if d["schedule"] and plan_start <= d["schedule"] <= plan_end
    )

    lines += [
        f"2. `{year2}년 하반기 DR 모의 훈련 (상반기 무중단 대상)",
        f"   1) 총 {h2_result['total']}대 中 {h2_result['done']}대 완료 "
        f"(진행률 {_fmt_rate(h2_result['rate'])}%)",
        "   2) 실적",
        f"      - 금주 실적 ({perf_start:%m/%d} ~ {perf_end:%m/%d}) : {perf_cnt}대",
        f"      - 차주 계획 ({plan_start:%m/%d} ~ {plan_end:%m/%d}) : {plan_cnt}대",
        "",
    ]

    return "\n".join(lines)


def send_report(half: str | None = None, use_jira: bool = True):
    text = build_report(half=half, use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
