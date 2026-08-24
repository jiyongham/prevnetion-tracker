# scripts/diag_capacity_unmatched.py
"""
용량관리 특정 항목(호스트명/CI명 부분 일치)이 왜 JIRA 티켓과 매칭이 안 되는지(또는
매칭은 됐는데 화면에 안 뜨는지) 확인.

DR훈련용 diag_unmatched_item.py와 같은 방식이되, 용량관리는 매칭 성공 이후에도
DATA/ARCH 소속 판정(classify_capacity_sheet)에서 한 번 더 걸러질 수 있어 그 단계도 같이 보여준다.

사용법:
  python -m scripts.diag_capacity_unmatched scdf-imalldb1
  python -m scripts.diag_capacity_unmatched scdf-imalldb1 --sheet ARCH   # 시트 지정 시 그 시트만
"""
import sys

from app.config import settings
from app.core.capacity_loader import load_capacity_items_merged
from app.core.jira_client import jira
from app.services.capacity import capacity_ticket_kind, classify_capacity_sheet
from app.services.completion import build_ticket_summary
from app.services.matcher import build_ip_index, parse_excel_ips


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--sheet")]
    sheet_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--sheet=")), None)
    keyword = " ".join(args).strip().lower()
    if not keyword:
        print('사용법: python -m scripts.diag_capacity_unmatched "호스트명 또는 CI명 일부" [--sheet=DATA|ARCH]')
        return

    sheets = [sheet_arg] if sheet_arg else ["DATA", "ARCH"]

    issues = jira.get_capacity_tickets()
    tickets = build_ticket_summary(issues, settings.planned_end_date_field, kind_fn=capacity_ticket_kind)
    ip_index = build_ip_index(tickets)
    print(f"(JQL로 조회된 용량관리 티켓 총 {len(tickets)}건 — 제목에 '예방4' 포함)\n")

    found_any = False
    for sheet in sheets:
        items = load_capacity_items_merged(sheet=sheet)
        hits = [
            i for i in items
            if keyword in (i.get("hostname") or "").lower()
            or keyword in (i.get("ci_name") or "").lower()
        ]
        if not hits:
            continue
        found_any = True

        for item in hits:
            print(f"=== [{sheet}] NO.{item['no']} {item['ci_name']} (대상 여부: {item['expand_flag'] or '미회신'}) ===")
            raw_ip = item.get("ip", "")
            parsed_ips = parse_excel_ips(raw_ip)
            print(f"엑셀 IP 원문: {raw_ip!r} -> 파싱: {parsed_ips}")

            matched_by_ip = []
            if not parsed_ips:
                print("  ⚠ IP가 비어있거나 파싱 실패 -> IP 매칭 자체가 불가능합니다.")
            for ip in parsed_ips:
                hit = ip_index.get(ip, [])
                matched_by_ip.extend(hit)
                print(f"  IP {ip} -> 이 IP를 포함한 티켓: {[t['key'] for t in hit] or '없음'}")

            host = (item.get("hostname") or "").strip().lower()
            print(f"엑셀 호스트명: {item.get('hostname')!r}")
            matched_by_host = []
            if len(host) < 4:
                print("  ⚠ 호스트명이 4자 미만이라 호스트명 매칭은 스킵됩니다.")
            else:
                matched_by_host = [t for t in tickets if host in t["match_text"].lower()]
                print(f"  호스트명 '{host}' 텍스트 포함 티켓: {[t['key'] for t in matched_by_host] or '없음'}")

            all_matched = list({t["key"]: t for t in (matched_by_ip + matched_by_host)}.values())
            if not all_matched:
                print("  -> IP/호스트명 어느 쪽으로도 매칭되는 티켓이 없습니다. "
                      "아직 JIRA 티켓이 안 만들어졌거나(제목에 '예방4' 없음), "
                      "변경작업 대상 필드에 이 IP/호스트명이 안 적혀 있을 가능성이 큽니다.")
            else:
                print("  -- 매칭된 티켓의 DATA/ARCH 소속 판정 (이 시트로 인정되는지) --")
                for t in all_matched:
                    cls = classify_capacity_sheet(t.get("match_text"))
                    mark = "O 이 시트로 인정됨" if sheet in cls else f"X 이 시트로 인정 안 됨 (판정: {cls or '보류(패턴 매칭 실패)'})"
                    print(f"    {t['key']} [{mark}] planned_end_date={t.get('planned_end_date')}")
                    if sheet not in cls:
                        print(f"        변경작업내용 일부: {(t.get('match_text') or '')[:200]!r}")
            print()

    if not found_any:
        print(f"'{keyword}' 이(가) 포함된 항목을 지정한 시트에서 찾지 못했습니다. "
              f"철자나 --sheet 지정을 확인하세요.")


if __name__ == "__main__":
    main()
