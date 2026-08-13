# scripts/test_match.py
"""IP 매칭 정확도 검증"""
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.core.jira_client import jira
from app.core.excel_loader import load_dr_items
from app.services.completion import build_ticket_summary, calc_dr_completion
from app.services.matcher import match_items_by_ip, parse_excel_ips


def main(half: str = "H2"):
    print("=" * 70)
    print("1. 엑셀 로드")
    print("=" * 70)
    items = load_dr_items(half=half)
    targets = [i for i in items if i["is_target"]]
    print(f"전체 {len(items)}행 / 대상(O) {len(targets)}건")

    # IP 파싱 확인
    no_ip = [i for i in targets if not parse_excel_ips(i.get("ip", ""))]
    if no_ip:
        print(f"⚠️ IP 없는 대상: {len(no_ip)}건")
        for i in no_ip[:5]:
            print(f"   - {i['no']} {i['system_name']} (IP값: {repr(i['ip'])})")

    print("\n" + "=" * 70)
    print("2. JIRA [예방3] 티켓 조회")
    print("=" * 70)
    issues = jira.get_dr_tickets()
    tickets = build_ticket_summary(issues, settings.planned_end_date_field)
    print(f"조회된 티켓: {len(tickets)}건")
    for t in tickets[:5]:
        print(f"   {t['key']} | 완료일:{t['planned_end_date']} | {t['summary'][:45]}")

    print("\n" + "=" * 70)
    print("3. IP 매칭")
    print("=" * 70)
    match_result = match_items_by_ip(targets, tickets)
    print(f"티켓에서 추출된 IP 종류: {match_result['ip_index_size']}개")
    print(f"✅ 매칭 성공: {len(match_result['matched'])}건")
    print(f"❌ 매칭 실패: {len(match_result['unmatched'])}건")

    if match_result["unmatched"]:
        print("\n[매칭 실패 목록]")
        for i in match_result["unmatched"][:10]:
            print(f"   - {i['no']} {i['system_name']} / {i['hostname']} / IP:{i['ip']}")

    print("\n" + "=" * 70)
    print("4. 완료율")
    print("=" * 70)
    result = calc_dr_completion(items, match_result, date.today())
    print(f"기준일: {result['as_of_date']}")
    print(f"완료율: {result['rate']}% ({result['done']}/{result['total']}건)")
    print(f"JIRA 매칭된 건: {result['matched_count']}건")


if __name__ == "__main__":
    half = sys.argv[1] if len(sys.argv) > 1 else "H2"
    main(half)
