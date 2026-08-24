# scripts/diag_capacity_remind_merge.py
"""
용량관리 미계획/미회신 리마인드에서, 같은 서버(CI명/호스트명)로 보이는 항목이
DATA/ARCH 양쪽 시트에 다 있는데도 한 통으로 안 합쳐지는 이유를 확인.

merge_same_server()는 (1) 병합 키(CI명, 없으면 호스트명/IP)가 같아야 하고,
(2) 그 전에 같은 운영팀(ops_team)으로 먼저 묶여야 한다 (group_and_vote 기준).
(3) 담당자(owner)가 비어있으면 애초에 리마인드 그룹핑에서 통째로 빠진다.
겉보기엔 같은 서버라도 이 셋 중 하나가 시트마다 미묘하게 다르면 안 합쳐진다 - 이 스크립트는
그 세 가지를 시트별로 나란히 보여준다 (repr로 찍어서 안 보이는 공백/문자 차이도 드러냄).

사용법:
  python -m scripts.diag_capacity_remind_merge "스타벅스 SAP ERP DB#1"
"""
import sys

from app.core.capacity_loader import load_capacity_items_merged
from app.services.capacity_reminder import _server_key
from app.services.reminder import parse_owners


def main():
    keyword = " ".join(sys.argv[1:]).strip().lower()
    if not keyword:
        print('사용법: python -m scripts.diag_capacity_remind_merge "CI명 또는 호스트명 일부"')
        return

    found_any = False
    for sheet in ("DATA", "ARCH"):
        items = load_capacity_items_merged(sheet=sheet)
        hits = [
            i for i in items
            if keyword in (i.get("ci_name") or "").lower()
            or keyword in (i.get("hostname") or "").lower()
        ]
        if not hits:
            print(f"[{sheet}] 매칭되는 행 없음\n")
            continue
        found_any = True

        for item in hits:
            owners = parse_owners(item.get("owner", ""))
            print(f"[{sheet}] NO.{item['no']}")
            print(f"  ci_name    = {item.get('ci_name')!r}")
            print(f"  hostname   = {item.get('hostname')!r}")
            print(f"  ip         = {item.get('ip')!r}")
            print(f"  ops_team   = {item.get('ops_team')!r}")
            print(f"  owner(raw) = {item.get('owner')!r} -> parsed={owners}")
            print(f"  expand_flag= {item.get('expand_flag')!r}")
            print(f"  병합키(server_key) = {_server_key(item)!r}")
            if not owners:
                print("  ⚠ 담당자(owner)가 비어있음 -> group_and_vote가 owner 없는 행은 건너뛰어서,")
                print("     이 행은 리마인드 그룹 자체에 아예 안 들어갑니다 (병합 문제 이전에 누락).")
            print()

    if not found_any:
        print(f"'{keyword}' 이(가) 포함된 행을 DATA/ARCH 어디서도 찾지 못했습니다.")
        return

    print(
        "-- 확인 포인트 --\n"
        "1) 두 시트의 '병합키(server_key)'가 글자 하나까지 완전히 같아야 병합됩니다.\n"
        "   (repr로 찍었으니 앞뒤 공백/전각공백 같은 안 보이는 차이도 여기서 드러납니다)\n"
        "2) '병합키'가 같아도 'ops_team'이 시트마다 다르면 애초에 다른 운영팀 그룹으로 갈려서\n"
        "   같은 메시지 안에서 만날 기회 자체가 없습니다.\n"
        "3) 둘 중 한쪽이 '⚠ 담당자가 비어있음'이면 그 행은 리마인드 대상에서 통째로 빠집니다."
    )


if __name__ == "__main__":
    main()
