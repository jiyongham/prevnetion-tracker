# app/services/capacity_report.py
from datetime import date

from app.config import settings
from app.core.capacity_loader import load_capacity_items_merged
from app.core.jira_client import jira
from app.core.teams_client import send_teams_message
from app.services.capacity import (
    build_capacity_ticket_summary,
    calc_capacity_completion,
    filter_tickets_by_sheet,
)
from app.services.matcher import match_items_by_ip


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


def _fmt_rate(rate: float) -> str:
    return f"{rate:g}"


def build_capacity_report(use_jira: bool = True) -> str:
    today = date.today()

    data_items, data_tmap = collect_capacity("DATA", use_jira)
    arch_items, arch_tmap = collect_capacity("ARCH", use_jira)
    all_items = data_items + arch_items

    # 증설 여부(O/X/공란) 기준 상태 집계. 공란(O도 X도 아님) = 미회신.
    planned_cnt = sum(1 for i in all_items if i["expand_flag"] == "O")
    excluded_cnt = sum(1 for i in all_items if i["expand_flag"] == "X")
    no_reply_cnt = sum(1 for i in all_items if i["expand_flag"] not in ("O", "X"))

    data_result = calc_capacity_completion(data_items, data_tmap, today)
    arch_result = calc_capacity_completion(arch_items, arch_tmap, today)
    total_target = data_result["total"] + arch_result["total"]
    total_done = data_result["done"] + arch_result["done"]
    rate = round(total_done / total_target * 100, 1) if total_target else 0.0

    lines = [
        "용량관리 현황 공유 드립니다.",
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
        f"       - 디스크 {total_target}대 中 {total_done}대 완료 (진행률 {_fmt_rate(rate)}%)",
    ]
    return "\n".join(lines)


def send_capacity_report(use_jira: bool = True):
    text = build_capacity_report(use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
