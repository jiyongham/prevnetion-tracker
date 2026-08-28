# app/services/capacity_report.py
from datetime import date

from app.config import settings
from app.core.capacity_loader import load_capacity_items_merged
from app.core.date_utils import week_ranges
from app.core.jira_client import jira
from app.core.teams_client import send_teams_message
from app.services.capacity import (
    build_capacity_ticket_summary,
    calc_capacity_completion,
    capacity_ticket_done_date,
    filter_tickets_by_sheet,
)
from app.services.ai_summary import generate_weekly_summary
from app.services.completion import fmt_rate, group_by
from app.services.matcher import match_items_by_ip
from app.services.report_check import check_report, record_sent


def _projected_done(result: dict, ticket_map: dict, today: date, cutoff: date) -> int:
    """
    완료 댓수 = 이미 완료된 대상 + 오늘부터 차주 말(cutoff)까지 완료 예정인 대상.
    (단순히 '차주 한 주'만 보면 일정이 그 주에 딱 걸리는 대상이 없을 때 숫자가 그대로라
    오늘~차주 말까지 누적으로 잡는다. 이미 지나버린(연체) 일정은 완료로 치지 않음.)
    """
    def _has_ticket_by(no):
        return any(
            (d := capacity_ticket_done_date(t)) and today <= d <= cutoff
            for t in (ticket_map.get(no) or [])
        )

    completed_nos = {d["no"] for d in result["details"] if d["completed"]}
    upcoming_nos = {
        d["no"] for d in result["details"]
        if _has_ticket_by(d["no"])
        or (d["schedule"] and today <= d["schedule"] <= cutoff)
    }
    return len(completed_nos | upcoming_nos)


def collect_capacity(sheet: str, use_jira: bool = True):
    items = load_capacity_items_merged(sheet=sheet)
    ticket_map = {}
    if use_jira:
        try:
            issues = jira.get_capacity_tickets()
            tickets = build_capacity_ticket_summary(issues, settings.planned_end_date_field)
            targets = [i for i in items if i["is_target"]]
            match_result = match_items_by_ip(targets, tickets)
            ticket_map = filter_tickets_by_sheet(match_result["matched"], sheet)
        except Exception as e:
            print(f"⚠️ 용량관리 JIRA 조회 실패 (엑셀 기준으로 계속): {e}")
    return items, ticket_map


def _build(use_jira: bool = True) -> tuple[str, dict]:
    """리포트 본문과 발송 전 점검/스냅샷용 집계를 함께 만든다"""
    today = date.today()

    data_items, data_tmap = collect_capacity("DATA", use_jira)
    arch_items, arch_tmap = collect_capacity("ARCH", use_jira)
    all_items = data_items + arch_items

    # 최종 상태(status_kind) 기준 집계. target=증설 예정(엑셀 O 또는 미회신+일정입력),
    # excluded=제외(엑셀 X 또는 웹 제외처리), no_reply=진짜 미회신(공란+일정없음).
    planned_cnt = sum(1 for i in all_items if i["status_kind"] == "target")
    excluded_cnt = sum(1 for i in all_items if i["status_kind"] == "excluded")
    no_reply_cnt = sum(1 for i in all_items if i["status_kind"] == "no_reply")

    data_result = calc_capacity_completion(data_items, data_tmap, today)
    arch_result = calc_capacity_completion(arch_items, arch_tmap, today)
    total_target = data_result["total"] + arch_result["total"]

    # 완료 댓수 = 오늘 기준 완료 + 오늘~차주 말까지 일정/티켓상 완료 예정인 대상
    _, perf_end, _, _ = week_ranges(today)
    total_done = (
        _projected_done(data_result, data_tmap, today, perf_end)
        + _projected_done(arch_result, arch_tmap, today, perf_end)
    )
    rate = round(total_done / total_target * 100, 1) if total_target else 0.0

    lines = [
        "[용량 관리]",
        "",
        f"1. 목적 : {settings.capacity_purpose}",
        "",
        "2. 내용",
        "   1) 데이터 저장 공간 사용률 80% 초과 시스템 디스크 증설",
        "   2) 변경 데이터 공간(Archive) 1일 변경량의 3배 확보",
        "",
        "3. 진행사항",
        f"   1) {settings.capacity_step1_label} : {settings.capacity_step1_date} (완료)",
        f"   2) {settings.capacity_step2_label} : {settings.capacity_step2_date} (완료)",
        f"      - 디스크증설 {settings.capacity_total_fixed}대 中 {planned_cnt}대 증설 예정",
        f"            ※ 프로젝트 진행 및 폐기 예정 등 {excluded_cnt}대 제외, 미회신 {no_reply_cnt}대",
        "   3) 디스크 증설 수행",
        f"       - 디스크 {total_target}대 中 {total_done}대 완료 (진행률 {fmt_rate(rate)}%)",
    ]

    # 특이사항 한 줄 (DR훈련 리포트와 같은 에이전트 공용).
    # 완료 판정은 위 total_done(차주 말까지 예정분 포함)과 달리 '오늘 기준 완료'만 보는
    # calc 결과를 그대로 쓴다 - 팀별 비교엔 예정분을 섞지 않는 게 맞다.
    merged = {
        "done": data_result["done"] + arch_result["done"],
        "total": total_target,
        "rate": round(
            (data_result["done"] + arch_result["done"]) / total_target * 100, 1
        ) if total_target else 0.0,
        "no_schedule": data_result["no_schedule"] + arch_result["no_schedule"],
    }
    by_team = group_by(
        {"details": data_result["details"] + arch_result["details"]}, "ops_team"
    )
    summary = generate_weekly_summary(
        "'26년 하반기 용량관리(DB 디스크 증설) 진척 현황", merged, by_team, unit="대"
    )
    if summary:
        lines += ["", "4. 이번 주 특이사항", f"   {summary}"]

    metrics = {
        "total": total_target,
        "done": total_done,
        "rate": rate,
        "no_schedule": merged["no_schedule"],
    }
    return "\n".join(lines), metrics


def build_capacity_report(use_jira: bool = True) -> str:
    return _build(use_jira=use_jira)[0]


def send_capacity_report(use_jira: bool = True) -> str | None:
    """
    리포트 발송. 발송 전 지난주 대비 이상 징후를 점검하고, 이상이 있어도 정기 보고라
    발송 자체는 막지 않고 경고만 반환한다 (호출부에서 로그/화면으로 알린다).
    """
    text, metrics = _build(use_jira=use_jira)
    warning = check_report("capacity", metrics)

    print(text)
    if warning:
        print(warning)
    print("-" * 60)

    send_teams_message(text)
    record_sent("capacity", metrics)
    return warning
