# scripts/rollback_bulk_save.py
"""
직전 '일괄 저장'(bulk-save) 배치를 되돌리는 스크립트.

change_log에는 필드별 old_value/new_value가 남아있으므로,
지정한 updated_by + half + 시간범위(since) 안에 있는 변경들을 모아
각 item_no별로 배치 이전 상태를 복원한다.

사용법 (기본은 dry-run, 실제 반영하려면 --apply):
  python -m scripts.rollback_bulk_save --by "홍길동" --half H2 --minutes 30
  python -m scripts.rollback_bulk_save --by "홍길동" --half H2 --minutes 30 --apply
"""
import argparse
import sys
from datetime import datetime, timedelta

from app.models.db import get_conn, get_input, upsert_input

FIELDS = ["schedule", "mode", "is_done", "evidence", "note"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", required=True, help="일괄 저장 시 사용한 입력자명 (updated_by)")
    ap.add_argument("--half", required=True, choices=["H1", "H2"])
    ap.add_argument("--minutes", type=int, default=30, help="최근 N분 이내 변경만 대상 (기본 30)")
    ap.add_argument("--apply", action="store_true", help="실제로 반영 (없으면 dry-run)")
    args = ap.parse_args()

    since = (datetime.now() - timedelta(minutes=args.minutes)).strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM change_log
            WHERE updated_by = ? AND half = ? AND updated_at >= ? AND field IN ({})
            ORDER BY item_no, id
            """.format(",".join("?" * len(FIELDS))),
            (args.by, args.half, since, *FIELDS),
        ).fetchall()

    if not rows:
        print(f"[스킵] {args.by} / {args.half} / 최근 {args.minutes}분 내 change_log 없음. --minutes 값을 늘려보세요.")
        return

    # item_no별로 필드마다 '이 배치에서 가장 먼저 바뀐' old_value = 배치 이전 원래 값
    per_item: dict[str, dict[str, str]] = {}
    for r in rows:
        d = dict(r)
        item = per_item.setdefault(d["item_no"], {})
        if d["field"] not in item:  # 가장 이른 old_value만 채택
            item[d["field"]] = d["old_value"]

    print(f"[대상] {len(per_item)}건 (updated_by={args.by}, half={args.half}, since={since})")
    for item_no, old_vals in per_item.items():
        current = get_input(item_no, args.half) or {}
        if "is_done" in old_vals:
            # old_value가 빈 문자열이면 '이전엔 DB 기록 자체가 없었음' -> False가 원래 상태
            is_done_restored = bool(int(old_vals["is_done"])) if old_vals["is_done"] != "" else False
        else:
            is_done_restored = bool(current.get("is_done"))

        restored = {
            "schedule": old_vals.get("schedule", current.get("schedule", "") or ""),
            "mode": old_vals.get("mode", current.get("mode", "") or ""),
            "is_done": is_done_restored,
            "evidence": old_vals.get("evidence", current.get("evidence", "") or ""),
            "note": old_vals.get("note", current.get("note", "") or ""),
        }
        changed_fields = list(old_vals.keys())
        print(f"  NO.{item_no}: 되돌릴 필드={changed_fields} -> {restored}")

        if args.apply:
            upsert_input(
                item_no=item_no,
                half=args.half,
                schedule=restored["schedule"],
                mode=restored["mode"],
                is_done=restored["is_done"],
                evidence=restored["evidence"],
                note=restored["note"],
                updated_by=f"rollback-by-{args.by}",
            )

    if not args.apply:
        print("\n[dry-run] 실제로 반영되지 않았습니다. 위 내용이 맞으면 --apply를 붙여 재실행하세요.")
    else:
        print(f"\n[완료] {len(per_item)}건 롤백 반영 (updated_by=rollback-by-{args.by} 로 기록됨)")


if __name__ == "__main__":
    main()
