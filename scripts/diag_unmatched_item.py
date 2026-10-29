# scripts/diag_unmatched_item.py
"""
특정 시스템명/호스트명(부분 일치)으로 대상 항목을 찾아 왜 JIRA 매칭이 안 되는지 확인.
DR 매칭은 티켓 작업 대상 CMDB 필드의 Key + IP/호스트명 텍스트의 합집합이다 (app/services/dr_match.py).

사용법:
  python -m scripts.diag_unmatched_item "유통VAN"
"""
import sys
from datetime import date

from app.config import settings
from app.core.date_utils import half_window
from app.core.excel_loader import get_targets, load_dr_items_merged
from app.core.jira_client import jira
from app.services.completion import ticket_done_date
from app.services.dr_match import build_dr_ticket_summary, match_by_cmdb_key, match_dr_tickets
from app.services.matcher import match_items_by_ip
from app.services.owner_check import lookup_cmdb_assets


def _cmdb_text(t: dict) -> str:
    return ", ".join(f"{c['name']} ({c['key']})" for c in t["cmdb_targets"]) or "비어 있음"


def main():
    keyword = " ".join(sys.argv[1:]).strip()
    if not keyword:
        print('사용법: python -m scripts.diag_unmatched_item "시스템명 또는 호스트명 일부"')
        return
    if not settings.dr_cmdb_target_field:
        print("⚠ DR_CMDB_TARGET_FIELD가 설정되지 않았습니다 - CMDB Key 매칭 없이 IP/호스트명으로만 매칭합니다.\n")

    # H2 대상은 엑셀 '대상 여부(O)' 전체다
    items = get_targets(load_dr_items_merged(half="H2"))
    kw = keyword.lower()
    hits = [i for i in items if kw in (i.get("system_name") or "").lower() or kw in (i.get("hostname") or "").lower()]
    if not hits:
        print(f"'{keyword}' 이(가) 시스템명/호스트명에 포함된 하반기 대상이 없습니다.")
        print("→ 철자를 다시 확인하거나, 엑셀 '대상 여부'가 O가 아닐 수 있습니다.")
        return

    tickets = build_dr_ticket_summary(jira.get_dr_tickets())
    print(f"(조회된 DR 티켓 {len(tickets)}건 — 제목에 '예방3'/'무중단' 또는 작업 구분 'DR훈련', "
          f"작업 대상 CMDB 필드 {settings.dr_cmdb_target_field})\n")
    w_start, w_end = half_window(date.today().year, "H2")
    print(f"하반기(H2) 판정 창: {w_start} ~ {w_end}\n")

    assets = lookup_cmdb_assets(hits)
    matched = match_dr_tickets(hits, tickets)
    by_key = match_by_cmdb_key(hits, tickets)
    by_text = match_items_by_ip(hits, tickets)["matched"]
    for item in hits:
        asset = assets.get(item["no"]) or {}
        print(f"=== NO.{item['no']} {item['system_name']} ===")
        print(f"엑셀 호스트명: {item.get('hostname')!r} / IP: {item.get('ip')!r}")
        if asset:
            print(f"  CMDB 자산: CI명={asset.get('label')!r} Key={asset.get('object_key')!r} 상태={asset.get('status')!r}")
        else:
            print("  ⚠ 이 호스트명으로 CMDB '서버' 자산을 못 찾았습니다 -> IP/호스트명 텍스트로만 매칭됩니다.")

        found = matched.get(item["no"], [])
        if not found:
            print("  매칭된 티켓: 없음")
        key_hits = {t["key"] for t in by_key.get(item["no"], [])}
        text_hits = {t["key"] for t in by_text.get(item["no"], [])}
        for t in found:
            dd = ticket_done_date(t)
            mark = "O 창 안(표시됨)" if dd and w_start <= dd <= w_end else "X 창 밖 or 완료일 없음(화면에 안 뜸)"
            how = "+".join(n for n, hit in (("Key", t["key"] in key_hits), ("IP/호스트명", t["key"] in text_hits)) if hit)
            print(f"  {t['key']} [{mark}] 매칭={how} kind={t['kind']} 완료일={dd} | 작업 대상: {_cmdb_text(t)}")
        print()

    # 이름은 언급됐는데 작업 대상 Key도, IP/전체 호스트명도 없는 티켓 (매칭 누락의 흔한 원인)
    matched_keys = {t["key"] for ts in matched.values() for t in ts}
    loose = [t for t in tickets if kw in t["match_text"].lower() and t["key"] not in matched_keys]
    print(f"'{keyword}'가 제목/본문에 있지만 매칭되지 않은 티켓 {len(loose)}건:")
    for t in loose:
        print(f"  {t['key']} | {t['summary'][:50]} | 작업 대상: {_cmdb_text(t)}")
    if loose:
        print("→ 이 티켓들의 작업 대상 CMDB 필드에 위 대상의 자산을 연결하거나, 변경작업 대상에 IP/전체 호스트명을 적으면 매칭됩니다.")


if __name__ == "__main__":
    main()
