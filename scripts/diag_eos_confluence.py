# scripts/diag_eos_confluence.py
"""
EoS 차주 계획을 Confluence 주간 작업계획 페이지에서 파악하는 로직 확인용.
아직 리포트/대시보드에는 연결하지 않았고, 이 스크립트로만 결과를 수동 확인한다.

사용법:
  python -m scripts.diag_eos_confluence                  # 이번 주 (오늘 기준 월~금)
  python -m scripts.diag_eos_confluence 2026-08-31        # 해당 날짜가 포함된 주
"""
import sys
from datetime import date, timedelta

from app.config import settings
from app.core.eos_loader import load_eos_items_merged
from app.services.eos_confluence import get_week_plan_count


def week_of(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=4)


def main():
    if len(sys.argv) > 1:
        base = date.fromisoformat(sys.argv[1])
    else:
        base = date.today()
    week_start, week_end = week_of(base)

    items = load_eos_items_merged()
    targets = [i for i in items if i["is_target"]]
    by_no = {i["item_no"]: i for i in items}

    result = get_week_plan_count(
        settings.confluence_eos_parent_page_id, week_start, week_end, targets
    )

    print(f"조회 주간: {week_start} ~ {week_end}")
    if not result["found"]:
        print("⚠ 아직 이 주 페이지가 Confluence에 없습니다 (미작성 상태일 수 있음)")
        return

    print(f"페이지: {result['page_title']} (id={result['page_id']})")
    print(f"IP전환 작업행 총 {result['row_count']}건 스캔 (EoS 무관 작업 포함될 수 있음)")
    print(f"  - EoS 대상 확정 매칭 {result['count']}대")
    print(f"  - 미매칭(EoS 무관이거나 수동 확인 필요) {len(result['unmatched_rows'])}건\n")

    if result["matched"]:
        print("[확정 매칭]")
        for item_no, row in result["matched"].items():
            it = by_no.get(item_no, {})
            print(f"  {item_no} {it.get('system_name', '?')} ({it.get('ops_team', '?')})")
            print(f"    작업자: {row['worker']} | {row['text']}")

    if result["unmatched_rows"]:
        print("\n[미매칭 - 수동 확인 필요]")
        for row in result["unmatched_rows"]:
            print(f"  작업자: {row['worker']} | {row['text']}")


if __name__ == "__main__":
    main()
