# app/services/capacity_report.py
from datetime import date

from app.config import settings
from app.core.agent_client import agent_chat, extract_answer
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
from app.services.completion import fmt_rate
from app.services.matcher import match_items_by_ip


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


def generate_weekly_summary(
    total_target: int,
    total_done: int,
    rate: float,
    data_result: dict,
    arch_result: dict,
    planned_cnt: int,
    excluded_cnt: int,
    no_reply_cnt: int,
) -> str | None:
    """
    현재 스냅샷(전체/DATA/ARCH 진행률 + 증설예정/제외/미응답 건수)만 근거로 '이번 주
    특이사항' 한 줄 생성. 에이전트 미설정/실패 시 None (리포트 발송 자체는 막지 않음).
    DR훈련(report.py)과 달리 팀별 완료율은 안 넘긴다 - 용량관리는 팀 단위 비교가
    의미가 없어서 뺌. 대신 DATA/ARCH가 따로 굴러가는 축이라 그 둘을 나눠서 준다.
    """
    if not settings.capacity_summary_agent_id:
        return None

    query = (
        "아래는 용량관리(디스크 증설) 하반기 진척 현황 스냅샷입니다. 이 데이터만 근거로, "
        "이번 리포트에서 눈에 띄는 특이사항을 한 문장으로 짚어주세요 "
        "(예: DATA/ARCH 중 한쪽만 유독 뒤처지는 점, 미응답/제외 비중이 큰 점, 전체적으로 "
        "순항 중이라는 점 등). "
        "과거 데이터가 없으니 추세(예: '몇 주째')는 절대 언급하지 말고, "
        "지금 데이터에 있는 사실만 쓰세요.\n\n"
        f"전체: {total_done}/{total_target}건 ({fmt_rate(rate)}%)\n"
        f"DATA(일반): {data_result['done']}/{data_result['total']}건 ({fmt_rate(data_result['rate'])}%)\n"
        f"ARCH(아카이브): {arch_result['done']}/{arch_result['total']}건 ({fmt_rate(arch_result['rate'])}%)\n"
        f"증설 예정 {planned_cnt}건, 제외 {excluded_cnt}건, 미응답 {no_reply_cnt}건"
    )
    try:
        r = agent_chat(
            user_id="system-report",
            query=query,
            agent_id=settings.capacity_summary_agent_id,
            agent_code=settings.capacity_summary_agent_code,
        )
        return extract_answer(r)
    except Exception as e:
        print(f"⚠️ 용량관리 주간 특이사항 생성 실패 (리포트는 정상 발송): {e}")
        return None


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


def build_capacity_report(use_jira: bool = True) -> str:
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
        "1. 목적 : '26년 하반기 그룹사 용량관리 기준 임계치 초과 DB서버 디스크 용량 증설",
        "",
        "2. 내용",
        "   1) 데이터 저장 공간 사용률 80% 초과 시스템 디스크 증설",
        "   2) 변경 데이터 공간(Archive) 1일 변경량의 3배 확보",
        "",
        "3. 진행사항",
        "   1) '26년 상반기 용량관리 대상 선별 및 공지 : 7/16 (완료)",
        "   2) 작업 일정 취합 및 진행 협의 : 7/31 (완료)",
        f"      - 디스크증설 86대 中 {planned_cnt}대 증설 예정",
        f"            ※ 프로젝트 진행 및 폐기 예정 등 {excluded_cnt}대 제외, 미회신 {no_reply_cnt}대",
        "   3) 디스크 증설 수행",
        f"       - 디스크 {total_target}대 中 {total_done}대 완료 (진행률 {fmt_rate(rate)}%)",
    ]

    # 특이사항 (AI 요약, 설정 안 했거나 실패하면 조용히 생략)
    summary = generate_weekly_summary(
        total_target, total_done, rate, data_result, arch_result,
        planned_cnt, excluded_cnt, no_reply_cnt,
    )
    if summary:
        lines += ["", "4. 특이사항", f"   {summary}"]

    return "\n".join(lines)


def send_capacity_report(use_jira: bool = True):
    text = build_capacity_report(use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
