# scripts/restore_dr_from_export.py
"""
DB 백업본(--base-db) 위에 더 최근의 DR 진척 엑셀 내보내기(/dr/export,
DR_진척_{half}_{날짜}.xlsx)를 덮어써 tracker.db를 복구한다.

  1) --base-db를 작업 사본으로 복사하고 현재 스키마로 맞춘다 (init_db).
     증적·비고·제외 사유·변경 이력·다른 반기/도메인은 모두 여기서 온다.
  2) 내보내기의 각 행을 '작업 사본 + 현재 엑셀 원본'으로 계산한 화면 값과 비교해
     다른 필드만 내보내기 값으로 맞춘다 (같은 항목이면 내보내기가 이긴다).
       - 일정     : 내보내기 값이 원본과 같으면 웹 입력을 지우고, 다르면 웹 입력으로 넣는다.
                    새로 X(웹 제외)가 된 건은 --admin 이름으로 기록해야 제외로 인정된다
                    (completion.excluded_completion_date). 이미 X였던 건은 처리자·처리일을 보존한다.
       - 담당자   : 일정과 같은 규칙
       - 수동완료 : 판정근거가 '완료표기'인지로 맞춘다. JIRA 완료는 수동 완료로 바꾸지 않는다.
       - 수행방식 : H2는 상반기 결과로 계산되므로 건드리지 않는다
  3) --apply면 운영 DB를 백업한 뒤 작업 사본으로 교체한다 (sqlite backup API라 앱이
     떠 있어도 반쯤 쓰인 파일을 읽는 일이 없다). dry-run이면 운영 DB는 그대로다.

내보내기로 바꾼 필드는 change_log에 복구 표시(updated_by)와 기준일 시각으로 남긴다.

사용법 (기본은 dry-run, 실제 반영하려면 --apply):
  python -m scripts.restore_dr_from_export data/DR_진척_H2_2026-10-20.xlsx --base-db data/tracker.local.db --admin "관리자"
  python -m scripts.restore_dr_from_export data/DR_진척_H2_2026-10-20.xlsx --base-db data/tracker.local.db --admin "관리자" --apply
"""
import argparse
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from app.config import settings
from app.core.date_utils import parse_schedule
from app.core.excel_loader import load_dr_items
from app.models import db
from app.services.completion import DONE_MARKS

_NAME_RE = re.compile(r"DR_진척_(H[12])_(\d{4}-\d{2}-\d{2})")


def _s(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _disp(raw: str, year: int) -> str:
    """completion.calc_completion의 schedule_disp와 같은 규칙"""
    sched = parse_schedule(raw, year)
    return f"{sched.month}/{sched.day}" if sched else (raw or "")


def _copy_db(src: Path, dst: Path) -> None:
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def _resolve(shown: str, current: str, original: str) -> tuple[str | None, str | None]:
    """화면 값(shown)이 되도록 웹 입력값을 정한다. 반환: (새 웹 입력값 또는 None=변경 없음, 경고)"""
    if shown == current:
        return None, None
    if shown == original:
        return "", None          # 웹 입력을 지우면 원본 값이 보인다
    if not shown:
        return None, "원본에 값이 있어 빈 값으로 만들 수 없음"
    return shown, None


def build_plans(df: pd.DataFrame, half: str, as_of: date, admin: str, marker: str):
    source = {i["no"]: i for i in load_dr_items(half=half)}
    existing = db.get_inputs(half)
    plans, warnings, missing = [], [], []

    for _, row in df.iterrows():
        no = _s(row.get("NO"))
        item = source.get(no)
        if not item:
            missing.append(no)
            continue
        cur = existing.get(no) or {}
        changes: dict = {}

        new_sched, warn = _resolve(
            _s(row.get("일정")),
            _disp(cur.get("schedule") or item["schedule_raw"], as_of.year),
            _disp(item["schedule_raw"], as_of.year),
        )
        if warn:
            warnings.append(f"NO.{no} 일정: {warn}")
        if new_sched is not None:
            changes["schedule"] = new_sched

        new_owner, warn = _resolve(_s(row.get("담당자")), cur.get("owner") or item["owner"], item["owner"])
        if warn:
            warnings.append(f"NO.{no} 담당자: {warn}")
        if new_owner is not None:
            changes["owner"] = new_owner

        if item["excel_done"].upper() not in DONE_MARKS:
            want_done = _s(row.get("판정근거")) == "완료표기"
            if want_done != bool(cur.get("is_done")):
                changes["is_done"] = int(want_done)

        if not changes:
            continue

        final_sched = changes.get("schedule", cur.get("schedule") or "")
        if final_sched.upper() == "X" and "schedule" not in changes:
            # 이미 제외된 건: 처리자·처리일이 제외 인정일을 정하므로 그대로 둔다
            updated_by, updated_at = cur.get("updated_by", ""), cur.get("updated_at", "")
        elif final_sched.upper() == "X":
            if not admin:
                warnings.append(f"NO.{no} 일정: 제외(X)는 --admin 없이 복구하지 않음")
                changes.pop("schedule")
                if not changes:
                    continue
                updated_by = marker
            else:
                updated_by = admin
            updated_at = f"{as_of} 00:00:00"
        else:
            updated_by, updated_at = marker, f"{as_of} 00:00:00"

        plans.append({"no": no, "before": cur, "changes": changes,
                      "updated_by": updated_by, "updated_at": updated_at})
    return plans, warnings, missing, len(source)


def apply_plans(plans: list[dict], half: str, as_of: date) -> None:
    logged_at = f"{as_of} 00:00:00"
    with db.get_conn() as conn:
        for p in plans:
            fields = {**p["changes"], "updated_by": p["updated_by"], "updated_at": p["updated_at"]}
            if p["before"]:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE schedule_input SET {sets} WHERE item_no = ? AND half = ?",
                             (*fields.values(), p["no"], half))
            else:
                cols = ["item_no", "half", "mode", "evidence", "note", "last_inputter", *fields]
                conn.execute(
                    f"INSERT INTO schedule_input ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    (p["no"], half, "", "", "", "", *fields.values()),
                )
            for field, new_v in p["changes"].items():
                old_v = p["before"].get(field, "")
                conn.execute("""
                    INSERT INTO change_log (item_no, half, field, old_value, new_value, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (p["no"], half, field, "" if old_v is None else str(old_v), str(new_v),
                      p["updated_by"], logged_at))


def _table_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    with sqlite3.connect(path) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export", help="DR_진척_{half}_{YYYY-MM-DD}.xlsx 경로")
    ap.add_argument("--base-db", help="바탕이 될 이전 tracker.db (없으면 현재 운영 DB 위에 덮어쓴다)")
    ap.add_argument("--admin", default="", help="새로 제외(X)된 건을 기록할 관리자 이름")
    ap.add_argument("--apply", action="store_true", help="실제로 반영 (없으면 dry-run)")
    args = ap.parse_args()

    export = Path(args.export)
    m = _NAME_RE.search(export.name)
    if not m:
        sys.exit(f"[중단] 파일명에서 반기/기준일을 읽지 못했습니다: {export.name}")
    half, as_of = m.group(1), date.fromisoformat(m.group(2))
    marker = f"복구({export.stem})"
    if args.admin and args.admin not in settings.admin_set:
        sys.exit(f"[중단] --admin '{args.admin}'이 관리자 목록에 없습니다 - X가 제외로 인정되지 않습니다")

    live = Path(settings.db_path)
    base = Path(args.base_db) if args.base_db else live
    if not base.exists():
        sys.exit(f"[중단] 바탕 DB가 없습니다: {base}")
    work = live.with_name(f"{live.stem}.restore-work{live.suffix}")
    work.unlink(missing_ok=True)
    _copy_db(base, work)

    try:
        db.DB_PATH = work          # 이후 db.* 호출은 전부 작업 사본을 본다
        db.init_db()               # 이전 스키마면 빠진 컬럼을 채운다
        plans, warnings, missing, source_rows = build_plans(
            pd.read_excel(export, dtype=str), half, as_of, args.admin, marker
        )

        print(f"[바탕] {base}")
        print(f"[덮어쓰기] {export.name} (half={half}, 기준일={as_of}, 엑셀 원본 {source_rows}행)")
        counts = {}
        for p in plans:
            for f in p["changes"]:
                counts[f] = counts.get(f, 0) + 1
        print(f"[대상] {len(plans)}건 변경 (필드별 {counts})")
        for p in plans:
            diff = ", ".join(f"{f}: {p['before'].get(f, '')!r}→{v!r}" for f, v in p["changes"].items())
            print(f"  NO.{p['no']}: {diff} (입력자={p['updated_by']})")
        for w in warnings:
            print(f"[경고] {w}")
        if missing:
            print(f"[경고] 현재 엑셀 원본에 없는 NO {len(missing)}건 - 건너뜀: {missing[:20]}")
        print("[주의] 엑셀 원본이 기준일 이후 바뀌었다면 그 변경분도 덮어쓰기 대상에 섞입니다.")

        if base != live:
            print(f"\n[교체 대상 운영 DB] {live} 현재 행 수: {_table_counts(live) or '(없음)'}")
            print("  → 이 DB의 내용은 바탕 DB + 내보내기 결과로 교체됩니다 (백업은 남김).")

        if not args.apply:
            print("\n[dry-run] 운영 DB는 바뀌지 않았습니다. 위 내용이 맞으면 --apply를 붙여 재실행하세요.")
            return

        apply_plans(plans, half, as_of)
        if live.exists():
            backup = live.with_name(f"{live.stem}.bak-{datetime.now():%Y%m%d-%H%M%S}{live.suffix}")
            _copy_db(live, backup)
            print(f"\n[백업] {backup}")
        _copy_db(work, live)
        print(f"[완료] {len(plans)}건 덮어쓰기 후 {live} 교체. 화면 캐시(최대 5분)가 남으니 파드를 재시작하세요.")
    finally:
        work.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
