# app/services/report.py
from datetime import date, timedelta

from app.config import settings
from app.core.jira_client import jira
from app.core.excel_loader import load_dr_items
from app.core.teams_client import send_teams_message
from app.services.completion import build_ticket_summary, calc_completion, group_by
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


def build_report(half: str | None = None, use_jira: bool = True) -> str:
    half = half or get_current_half()
    half_label = "상반기" if half == "H1" else "하반기"

    items, ticket_map = collect(half, use_jira)

    today = date.today()
    next_week = today + timedelta(days=7)

    now_result = calc_completion(items, ticket_map, today)
    next_result = calc_completion(items, ticket_map, next_week)

    lines = [
        f"## 🔄 DR 모의훈련 진척 현황 ({half_label})",
        f"기준일: {today}",
        "",
        f"**현재 완료율: {now_result['rate']}% "
        f"({now_result['done']}/{now_result['total']}건)**",
        f"다음주({next_week}) 예정 포함: {next_result['rate']}% "
        f"({next_result['done']}/{next_result['total']}건)",
        "",
        "### 운영팀별 현황",
    ]

    by_team = group_by(now_result, "ops_team")
    for team, v in sorted(by_team.items(), key=lambda x: (x[1]["rate"], x[0])):
        icon = "🟢" if v["rate"] == 100 else ("🟡" if v["rate"] >= 50 else "🔴")
        lines.append(f"{icon} {team}: {v['rate']}% ({v['done']}/{v['total']})")

    # 미완료 목록
    pending = [d for d in now_result["details"] if not d["completed"]]
    if pending:
        lines += ["", f"### ⚠️ 미완료 {len(pending)}건"]
        for p in sorted(pending, key=lambda x: (x["schedule"] or date.max))[:20]:
            sched = p["schedule_raw"] or "일정미정"
            lines.append(
                f"- {p['system_name']} / {p['hostname']} "
                f"({p['ops_team']}, {p['owner']}, {sched})"
            )
        if len(pending) > 20:
            lines.append(f"- ... 외 {len(pending) - 20}건")

    if now_result["no_schedule"]:
        lines += ["", f"📌 일정 미입력: {now_result['no_schedule']}건"]

    return "\n".join(lines)


def send_report(half: str | None = None, use_jira: bool = True):
    text = build_report(half=half, use_jira=use_jira)
    print(text)
    print("-" * 60)
    send_teams_message(text)
