# app/services/report.py
from datetime import date

from app.config import settings
from app.core.date_utils import week_ranges
from app.core.jira_client import jira
from app.core.excel_loader import get_targets, load_dr_items_merged, scope_h2_targets
from app.core.teams_client import send_teams_message
from app.services.completion import (
    DONE_MARKS,
    build_ticket_summary,
    calc_completion,
    fmt_rate,
    group_by,
    ticket_done_date,
)
from app.services.ai_summary import generate_weekly_summary
from app.services.matcher import match_items_by_ip
from app.services.report_check import check_report, record_sent


def get_current_half() -> str:
    """현재 반기 자동 판별"""
    return "H1" if date.today().month <= 6 else "H2"


def collect(half: str, use_jira: bool = True):
    items = load_dr_items_merged(half=half)
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


def _build(half: str | None = None, use_jira: bool = True) -> tuple[str, dict]:
    """리포트 본문과 발송 전 점검/스냅샷용 집계를 함께 만든다"""
    today = date.today()
    year2 = str(today.year)[2:]  # 2026 -> "26"

    h2_items, h2_tmap = collect("H2", use_jira)

    # 하반기 대상 = 상반기 무중단으로 수행한 대상에 한함
    h2_scope = scope_h2_targets(h2_items)
    h2_result = calc_completion(h2_scope, h2_tmap, today)

    # ── 1. 전체 대상 ──
    total_all = len(h2_items)
    target_cnt = len(get_targets(h2_items))
    excluded = total_all - target_cnt

    lines = [
        "[DR훈련]",
        "",
        "1. 전체 대상",
        f"-  총 {total_all}대 中 {target_cnt}대, 제외 {excluded}대",
        f"    ※ {settings.dr_excluded_reason}으로 대상 제외 {excluded}대",
        "",
    ]

    # ── 2. 상반기 (종료된 반기 - 엑셀 완료표기 기준) ──
    # 방식 내역은 '완료' 기준이 아니라 '대상' 기준으로 센다. 그래야 실전환+무중단이
    # 항상 대상 수와 맞고, 여기 무중단 수가 곧 아래 하반기 분모라는 게 드러난다.
    h1_items = load_dr_items_merged(half="H1")
    h1_targets = get_targets(h1_items)
    h1_total = len(h1_targets)
    h1_real = settings.dr_h1_real if settings.dr_h1_real is not None else sum(
        1 for i in h1_targets if "실전환" in (i.get("mode") or "")
    )
    h1_nonstop = settings.dr_h1_nonstop if settings.dr_h1_nonstop is not None else sum(
        1 for i in h1_targets if "무중단" in (i.get("mode") or "")
    )
    # 상반기는 이미 종료됐으므로 엑셀 완료표기만 집계. 엑셀 기입이 누락된 경우를 위해
    # DR_H1_DONE으로 확정값을 덮어쓸 수 있다.
    h1_done = settings.dr_h1_done if settings.dr_h1_done is not None else sum(
        1 for i in h1_targets if (i.get("excel_done") or "").upper() in DONE_MARKS
    )
    h1_rate = round(h1_done / h1_total * 100, 1) if h1_total else 0.0

    lines += [
        f"2. '{year2}년 상반기 DR 모의 훈련 진행",
        f"   1) 총 {h1_total}대 中 {h1_done}대 완료 (진행률 {fmt_rate(h1_rate)}%)",
        f"       ※ 실전환 : {h1_real}대, 무중단 : {h1_nonstop}대",
        "",
    ]

    # ── 3. 하반기 (상반기 무중단 대상 / 월 일정·주간 실적·계획) ──
    perf_start, perf_end, plan_start, plan_end = week_ranges(today)

    # 금주 실적: 다음 주에 완료일(실전환=변경계획완료일 / 무중단=생성일)이 있는 대상 수.
    # JIRA 티켓이 아직 매칭 안 됐어도, 입력된 일정이 해당 주에 있으면 계획으로라도 집계.
    def _has_ticket_in(nos_tickets, start, end):
        return any(
            (d := ticket_done_date(t)) and start <= d <= end
            for t in (nos_tickets or [])
        )

    completed_nos = {d["no"] for d in h2_result["details"] if d["completed"]}
    perf_nos = {
        d["no"] for d in h2_result["details"]
        if _has_ticket_in(h2_tmap.get(d["no"]), perf_start, perf_end)
        or (d["schedule"] and perf_start <= d["schedule"] <= perf_end)
    }
    perf_cnt = len(perf_nos)

    # 총 완료/진행률: 현재까지 완료 + 금주 실적을 합쳐서 표시 (같은 대상 중복 집계 방지)
    projected_done = len(completed_nos | perf_nos)
    projected_rate = round(projected_done / h2_result["total"] * 100, 1) if h2_result["total"] else 0.0

    # 차주 계획: 그 다음 주에 웹/엑셀 등록 일정이 잡힌 대수
    plan_cnt = sum(
        1 for d in h2_result["details"]
        if d["schedule"] and plan_start <= d["schedule"] <= plan_end
    )

    # 이번 달 일정: 한 건도 없으면 "N월 일정 없음"으로 알린다
    month_cnt = sum(
        1 for d in h2_result["details"]
        if d["schedule"] and d["schedule"].year == today.year
        and d["schedule"].month == today.month
    )
    month_line = (
        f"   2) {today.month}월 일정 없음" if not month_cnt
        else f"   2) {today.month}월 일정 : {month_cnt}대"
    )

    lines += [
        f"3. '{year2}년 하반기 DR 모의 훈련",
        f"   1) 총 {h2_result['total']}대 中 {projected_done}대 완료 "
        f"(진행률 {fmt_rate(projected_rate)}%)",
        month_line,
        "   3) 실적",
        f"      - 금주 실적 ({perf_start.month}/{perf_start.day} ~ {perf_end.month}/{perf_end.day}) : {perf_cnt}대",
        f"      - 차주 계획 ({plan_start.month}/{plan_start.day} ~ {plan_end.month}/{plan_end.day}) : {plan_cnt}대",
        "",
    ]

    # ── 4. 이번 주 특이사항 (AI 요약, 설정 안 했거나 실패하면 조용히 생략) ──
    by_team = group_by(h2_result, "ops_team")
    summary = generate_weekly_summary("DR 모의훈련 하반기 진척 현황", h2_result, by_team)
    if summary:
        lines += ["4. 이번 주 특이사항", f"   {summary}", ""]

    metrics = {
        "total": h2_result["total"],
        "done": projected_done,
        "rate": projected_rate,
        "no_schedule": h2_result["no_schedule"],
        "perf_cnt": perf_cnt,
        "plan_cnt": plan_cnt,
    }
    return "\n".join(lines), metrics


def build_report(half: str | None = None, use_jira: bool = True) -> str:
    return _build(half=half, use_jira=use_jira)[0]


def send_report(half: str | None = None, use_jira: bool = True) -> str | None:
    """
    리포트 발송. 발송 전 지난주 대비 이상 징후를 점검하고, 이상이 있어도 정기 보고라
    발송 자체는 막지 않고 경고만 반환한다 (호출부에서 로그/화면으로 알린다).
    """
    text, metrics = _build(half=half, use_jira=use_jira)
    warning = check_report("dr", metrics)

    print(text)
    if warning:
        print(warning)
    print("-" * 60)

    send_teams_message(text)
    record_sent("dr", metrics)
    return warning
