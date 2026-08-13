# scripts/test_jira.py
import sys
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.jira_client import jira
from app.services.completion import build_ticket_summary, calc_completion_rate


def test_full_flow():
    print("=" * 60)
    print("1. JIRA에서 [예방N] 티켓 조회")
    print("=" * 60)

    issues = jira.get_prevention_tickets()
    tickets = build_ticket_summary(issues)

    for t in tickets:
        print(f"{t['key']} | {t['prevention_type']} | {t['status']} | "
              f"완료예정일: {t['planned_end_date']} | {t['summary'][:40]}")

    print(f"\n총 {len(tickets)}건 조회됨\n")

    # 2. 임시로 엑셀 대체 (실제론 excel_loader에서 로드)
    print("=" * 60)
    print("2. 완료율 계산 (임시 - 조회된 티켓을 계획목록으로 가정)")
    print("=" * 60)

    ticket_map = {t["key"]: t for t in tickets}
    fake_planned_items = [
        {"service_name": "테스트", "prevention_type": t["prevention_type"],
         "jira_ticket_key": t["key"]}
        for t in tickets
    ]

    today = date.today()
    result = calc_completion_rate(fake_planned_items, ticket_map, today)

    print(f"기준일: {result['as_of_date']}")
    print(f"전체: {result['total']}건 / 완료: {result['done']}건 / 완료율: {result['rate']}%")


if __name__ == "__main__":
    test_full_flow()
