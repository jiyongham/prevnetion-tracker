# scripts/diag_eos_polestar.py
"""
Polestar CI명의 '_OLD' 접미사 기준 EoS 실제 전환 완료 현황 확인용.

JIRA 티켓 기준(변경계획시작일) 판정과 나란히 찍어서 차이를 눈으로 비교한다.
아직 리포트/대시보드에는 연결하지 않았고, 이 스크립트로만 수동 확인한다.

사용법:
  python -m scripts.diag_eos_polestar          # 요약만
  python -m scripts.diag_eos_polestar -v       # 전환완료/미연결 상세 목록까지
"""
import sys
from datetime import date

from app.core.polestar_client import polestar
from app.services.eos import calc_eos_completion, filter_track
from app.services.eos_polestar import check_eos_conversion
from app.services.eos_report import DB_TOTAL_FIXED, OS_TOTAL_FIXED, collect_eos


def main():
    verbose = "-v" in sys.argv

    resources = polestar.list_resources()
    print(f"Polestar 리소스 {len(resources)}건 조회\n")

    items, ticket_map = collect_eos(use_jira=True)

    for track, fixed_total in (("OS", OS_TOTAL_FIXED), ("DB", DB_TOTAL_FIXED)):
        track_items = filter_track(items, track)
        jira_result = calc_eos_completion(track_items, ticket_map, date.today())
        targets = [i for i in track_items if i.get("is_target")]
        conv = check_eos_conversion(targets, resources)

        print(f"[{track}] 대상 {len(targets)}대 (리포트 고정 모수 {fixed_total}대)")
        print(f"  JIRA 티켓 기준 완료 : {jira_result['done']}대  (변경계획시작일 기준이라 실제 완료보다 많이 잡힘)")
        print(f"  Polestar 전환 완료  : {len(conv['converted'])}대")
        print(f"     - TO-BE(_NEW) 준비됨(미전환) : {len(conv['pending_new'])}대")
        print(f"     - 미착수                     : {len(conv['not_started'])}대")
        print(f"     - Polestar 미등록(수동 확인) : {len(conv['unlinked'])}대")

        reasons: dict[str, int] = {}
        for c in conv["converted"]:
            key = c["polestar_reason"].split(" (")[0]
            reasons[key] = reasons.get(key, 0) + 1
        print(f"     - 판정 근거별: {reasons}")

        if verbose:
            print("\n  [전환 완료]")
            for c in conv["converted"]:
                print(f"    {c['system_name']}  ({c['polestar_reason']})")
            print("\n  [Polestar 미등록 - 수동 확인 필요]")
            for u in conv["unlinked"]:
                print(f"    {u['system_name']}  host={u['hostname']}  ip={u['ip']}")
        print()


if __name__ == "__main__":
    main()
