# scripts/diag_unmatched_item.py
"""
특정 시스템명(부분 일치)으로 대상 항목을 찾아 왜 JIRA 매칭이 안 되는지 확인.
매칭은 IP 우선, 실패 시 호스트명(4자 이상) 텍스트 포함 여부로 이뤄진다 (app/services/matcher.py).

사용법:
  python -m scripts.diag_unmatched_item "MMS 쿠폰"
"""
import sys

from datetime import date

from app.config import settings
from app.core.date_utils import half_window
from app.core.excel_loader import get_targets, load_dr_items_merged
from app.core.jira_client import jira
from app.services.completion import build_ticket_summary, ticket_done_date
from app.services.matcher import build_ip_index, parse_excel_ips


def main():
    keyword = " ".join(sys.argv[1:]).strip()
    if not keyword:
        print('사용법: python -m scripts.diag_unmatched_item "시스템명 일부"')
        return

    # H2 대상은 엑셀 '대상 여부(O)' 전체다 (상반기 무중단으로 좁히던 scope_h2_targets는 제거됨)
    items = get_targets(load_dr_items_merged(half="H2"))
    kw = keyword.lower()
    hits = [i for i in items if kw in (i.get("system_name") or "").lower() or kw in (i.get("hostname") or "").lower()]
    if not hits:
        print(f"'{keyword}' 이(가) 시스템명/호스트명에 포함된 하반기 대상이 없습니다.")
        print("→ 철자를 다시 확인하거나, 엑셀 '대상 여부'가 O가 아닐 수 있습니다.")
        return

    issues = jira.get_dr_tickets()
    tickets = build_ticket_summary(issues, settings.planned_end_date_field)
    ip_index = build_ip_index(tickets)
    print(f"(JQL로 조회된 DR 관련 티켓 총 {len(tickets)}건 — 제목에 '예방3'/'무중단' 또는 작업 구분 'DR훈련')\n")

    today = date.today()
    w_start, w_end = half_window(today.year, "H2")
    print(f"하반기(H2) 판정 창: {w_start} ~ {w_end}\n")

    def _describe(t):
        dd = ticket_done_date(t)
        in_win = bool(dd and w_start <= dd <= w_end)
        mark = "O 창 안(표시됨)" if in_win else "X 창 밖 or 완료일 없음(화면에 안 뜸)"
        return (
            f"{t['key']} [{mark}] kind={t['kind']} "
            f"planned_end_date={t.get('planned_end_date')} created_date={t.get('created_date')} "
            f"-> ticket_done_date={dd}"
        )

    for item in hits:
        print(f"=== NO.{item['no']} {item['system_name']} ===")
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
            print("  ⚠ 호스트명이 4자 미만이라 호스트명 매칭은 스킵됩니다 (MIN_HOSTNAME_LEN=4).")
        else:
            matched_by_host = [t for t in tickets if host in t["match_text"].lower()]
            print(f"  호스트명 '{host}' 텍스트 포함 티켓: {[t['key'] for t in matched_by_host] or '없음'}")

        all_matched = {t["key"]: t for t in (matched_by_ip + matched_by_host)}.values()
        if all_matched:
            print("  -- 매칭된 티켓의 완료판정/화면표시 여부 --")
            for t in all_matched:
                print(f"    {_describe(t)}")
        print()

    # 시스템명 키워드로 티켓 제목/본문 직접 검색 (애초에 티켓이 존재하는지, 제목이 다른지 확인용)
    kw_hits = [
        (t["key"], t["summary"]) for t in tickets
        if keyword.lower() in t["match_text"].lower()
    ]
    print(f"'{keyword}' 텍스트가 포함된 JIRA 티켓(제목/본문/변경작업대상): {kw_hits or '없음'}")
    if not kw_hits:
        print("→ 조회된 티켓 중 이 이름이 언급된 게 전혀 없다면, 해당 시스템의 변경 티켓이 아직")
        print("  안 만들어졌거나(제목에 '예방3'/'무중단'도, 작업 구분 'DR훈련'도 없거나) 다른 이름으로 등록됐을 가능성이 큽니다.")


if __name__ == "__main__":
    main()
