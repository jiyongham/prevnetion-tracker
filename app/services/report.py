# app/services/report.py
from datetime import date, timedelta

from app.config import settings
from app.core.agent_client import agent_chat, extract_answer
from app.core.jira_client import jira
from app.core.excel_loader import get_targets, load_dr_items_merged, scope_h2_targets
from app.core.teams_client import send_teams_message
from app.services.completion import (
    build_ticket_summary,
    calc_completion,
    group_by,
    ticket_done_date,
)
from app.services.matcher import match_items_by_ip


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


def generate_weekly_summary(result: dict, by_team: dict) -> str | None:
    """
    현재 스냅샷(팀별 완료율)만 근거로 '이번 주 특이사항' 한 줄 생성.
    에이전트 미설정/실패 시 None (리포트 발송 자체는 막지 않음).
    """
    if not settings.summary_agent_id:
        return None

    team_lines = "\n".join(
        f"- {team}: {v['done']}/{v['total']}건 ({v['rate']}%)"
        for team, v in sorted(by_team.items(), key=lambda x: x[1]["rate"])
    )
    query = (
        "아래는 DR 모의훈련 하반기 진척 현황 스냅샷입니다. 이 데이터만 근거로, "
        "이번 리포트에서 눈에 띄는 특이사항을 한 문장으로 짚어주세요 "
        "(예: 진척이 유독 느린 팀, 미계획 비중이 큰 점 등). "
        "과거 데이터가 없으니 추세(예: '몇 주째')는 절대 언급하지 말고, "
        "지금 데이터에 있는 사실만 쓰세요.\n\n"
        f"전체: {result['done']}/{result['total']}건 ({result['rate']}%), "
        f"미계획 {result['no_schedule']}건\n"
        f"팀별 현황(낮은 순):\n{team_lines}"
    )
    try:
        r = agent_chat(
            user_id="system-report",
            query=query,
            agent_id=settings.summary_agent_id,
            agent_code=settings.summary_agent_code,
        )
        return extract_answer(r)
    except Exception as e:
        print(f"⚠️ 주간 특이사항 생성 실패 (리포트는 정상 발송): {e}")
        return None


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

    # ── 3. 이번 주 특이사항 (AI 요약, 설정 안 했거나 실패하면 조용히 생략) ──
    by_team = group_by(h2_result, "ops_team")
    summary = generate_weekly_summary(h2_result, by_team)
    if summary:
        lines += ["3. 이번 주 특이사항", f"   {summary}", ""]

    return "\n".join(lines)


def send_report(half: str | None = None, use_jira: bool = True):
    text = build_report(half=half, use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
