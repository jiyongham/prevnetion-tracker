#!/usr/bin/env python3
# scripts/backup_db.py
"""
운영 파드의 tracker.db를 로컬(WSL)에 백업한다. kind 클러스터를 다시 만들면 PVC가 같이
사라지므로 백업은 반드시 클러스터 밖에 둔다. 표준 라이브러리만 쓰므로 앱 의존성 없이
`python3 scripts/backup_db.py`로 돌린다 (kubectl이 PATH에 있어야 한다).

  - 풀 백업 : 주 1회 (기본 일요일). 그 주의 기준이 된다.            full/tracker-YYYYMMDD-HHMM.db.gz
  - 증분 백업: 나머지 날. SQLite에는 증분 백업 기능이 없어서, 직전 풀 백업과 비교해
              바뀐 행만 SQL로 남긴다(차등). 복원은 풀 + 그 SQL 하나면 된다.   diff/tracker-YYYYMMDD-HHMM.sql.gz
              만든 SQL을 풀 백업에 실제로 적용해 원본과 같은지 확인하고, 다르거나 스키마가
              바뀌었으면 그날은 풀 백업으로 대신한다.
  - 보관    : 14일. 남아 있는 증분이 기준으로 삼는 풀 백업은 14일이 지나도 지우지 않는다.

사용법:
  python3 scripts/backup_db.py                       # 오늘 몫 (풀 또는 증분 자동 선택)
  python3 scripts/backup_db.py --full                # 강제 풀 백업
  python3 scripts/backup_db.py restore <백업파일> --out restored.db
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DEST = Path.home() / "dr-tracker-backups"
REMOTE_DB = "/app/data/tracker.db"
REMOTE_SNAPSHOT = "/app/data/.backup-snapshot.db"
BASE_HEADER = "-- base: "


# ── 파드에서 일관된 스냅샷 받기 ───────────────────────────────────────────────
def _kubectl(namespace: str, *args: str, binary: bool = False) -> bytes | str:
    out = subprocess.run(["kubectl", "-n", namespace, *args], check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode().strip()


def fetch_snapshot(namespace: str, selector: str, dest: Path) -> None:
    pod = _kubectl(namespace, "get", "pod", "-l", selector, "--field-selector=status.phase=Running",
                   "-o", "jsonpath={.items[0].metadata.name}")
    if not pod:
        raise RuntimeError(f"실행 중인 파드가 없습니다 ({namespace}, {selector})")
    # 파일을 그대로 복사하면 쓰는 중인 DB가 깨진 채 올 수 있다 - sqlite backup API로 떠서 받는다.
    script = (
        "import sqlite3;"
        f"s=sqlite3.connect('{REMOTE_DB}');d=sqlite3.connect('{REMOTE_SNAPSHOT}');"
        "s.backup(d);d.close();s.close()"
    )
    try:
        _kubectl(namespace, "exec", pod, "--", "python", "-c", script)
        dest.write_bytes(_kubectl(namespace, "exec", pod, "--", "cat", REMOTE_SNAPSHOT, binary=True))
    finally:
        subprocess.run(["kubectl", "-n", namespace, "exec", pod, "--", "rm", "-f", REMOTE_SNAPSHOT],
                       capture_output=True)
    with closing(sqlite3.connect(dest)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("받은 스냅샷의 무결성 검사에 실패했습니다")


# ── 차등 SQL ────────────────────────────────────────────────────────────────
def _tables(conn) -> list[str]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    has_seq = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
    return names + (["sqlite_sequence"] if has_seq else [])   # AUTOINCREMENT 카운터도 맞춘다


def _schema(conn) -> list[tuple]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()


def _rows(conn, table: str) -> tuple[list[str], dict]:
    cur = conn.execute(f'SELECT rowid, * FROM "{table}"')
    cols = [d[0] for d in cur.description][1:]
    return cols, {r[0]: r[1:] for r in cur}


def _rowid_alias(conn, table: str) -> bool:
    """'id INTEGER PRIMARY KEY'처럼 rowid의 별칭 컬럼이 있으면 그 컬럼이 곧 rowid다."""
    pks = [r for r in conn.execute(f'PRAGMA table_info("{table}")') if r[5]]
    return len(pks) == 1 and pks[0][2].upper() == "INTEGER"


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return f"X'{v.hex()}'"
    return "'" + str(v).replace("'", "''") + "'"


def make_diff(base: Path, current: Path) -> str | None:
    """base -> current 로 만드는 SQL. 스키마가 다르면 None (풀 백업으로 대신한다)."""
    with closing(sqlite3.connect(base)) as b, closing(sqlite3.connect(current)) as c:
        if _schema(b) != _schema(c):
            return None
        lines = ["BEGIN;"]
        for table in _tables(c):
            cols, new = _rows(c, table)
            _, old = _rows(b, table)
            gone = [rid for rid in old if rid not in new]
            for i in range(0, len(gone), 500):
                lines.append(f'DELETE FROM "{table}" WHERE rowid IN ({",".join(map(str, gone[i:i + 500]))});')
            # rowid 별칭 컬럼이 있으면 그 컬럼에 값이 들어가므로 rowid를 따로 적지 않는다
            alias = _rowid_alias(c, table)
            col_list = ", ".join([*([] if alias else ["rowid"]), *(f'"{col}"' for col in cols)])
            for rid, row in new.items():
                if old.get(rid) != row:
                    values = ", ".join([*([] if alias else [str(rid)]), *map(_literal, row)])
                    lines.append(f'INSERT OR REPLACE INTO "{table}" ({col_list}) VALUES ({values});')
        lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with closing(sqlite3.connect(path)) as conn:
        for table in _tables(conn):
            h.update(table.encode())
            for row in conn.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid'):
                h.update(repr(row).encode())
    return h.hexdigest()


# ── 파일 ────────────────────────────────────────────────────────────────────
def _gunzip_to(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as f, open(dst, "wb") as out:
        shutil.copyfileobj(f, out)


def _write_private(path: Path, data: bytes) -> None:
    # 담당자 이름·호스트명이 들어 있다 - 본인만 읽게 둔다.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _stamp(path: Path) -> datetime:
    return datetime.strptime(path.name.split(".")[0].removeprefix("tracker-"), "%Y%m%d-%H%M")


def _base_of(diff_file: Path) -> str:
    with gzip.open(diff_file, "rt", encoding="utf-8") as f:
        first = f.readline().strip()
    return first.removeprefix(BASE_HEADER) if first.startswith(BASE_HEADER) else ""


def restore(backup: Path, out: Path) -> None:
    dest = backup.parent.parent
    if backup.parent.name == "full":
        _gunzip_to(backup, out)
    else:
        base = dest / _base_of(backup)
        if not base.exists():
            sys.exit(f"[중단] 기준 풀 백업이 없습니다: {base}")
        _gunzip_to(base, out)
        with closing(sqlite3.connect(out)) as conn, gzip.open(backup, "rt", encoding="utf-8") as f:
            conn.executescript(f.read())
    print(f"[복원] {backup.name} -> {out}")


def prune(dest: Path, keep_days: int, now: datetime) -> None:
    cutoff = now - timedelta(days=keep_days)
    diffs = sorted((dest / "diff").glob("tracker-*.sql.gz"))
    for d in diffs:
        if _stamp(d) < cutoff:
            d.unlink()
    needed = {_base_of(d) for d in (dest / "diff").glob("tracker-*.sql.gz")}
    fulls = sorted((dest / "full").glob("tracker-*.db.gz"))
    for f in fulls[:-1]:   # 가장 최근 풀 백업은 항상 남긴다
        if _stamp(f) < cutoff and f"full/{f.name}" not in needed:
            f.unlink()


def backup(args) -> None:
    dest: Path = args.dest
    for sub in ("full", "diff"):
        (dest / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now()
    name = f"tracker-{now:%Y%m%d-%H%M}"

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "snapshot.db"
        fetch_snapshot(args.namespace, args.selector, snapshot)

        fulls = sorted((dest / "full").glob("tracker-*.db.gz"))
        last_full = fulls[-1] if fulls else None
        want_full = (
            args.full or last_full is None
            or now.weekday() == args.full_weekday
            or now - _stamp(last_full) >= timedelta(days=7)
        )

        sql = None
        if not want_full:
            base_db = Path(tmp) / "base.db"
            _gunzip_to(last_full, base_db)
            sql = make_diff(base_db, snapshot)
            if sql is None:
                print("[알림] 스키마가 바뀌어 풀 백업으로 대신합니다")
            else:
                # 만든 SQL이 실제로 원본을 재현하는지 확인한 뒤에만 증분으로 남긴다
                with closing(sqlite3.connect(base_db)) as conn:
                    conn.executescript(sql)
                if _content_hash(base_db) != _content_hash(snapshot):
                    print("[경고] 증분 검증 실패 - 풀 백업으로 대신합니다")
                    sql = None

        if sql is None:
            path = dest / "full" / f"{name}.db.gz"
            _write_private(path, gzip.compress(snapshot.read_bytes()))
            kind = "풀"
        else:
            path = dest / "diff" / f"{name}.sql.gz"
            body = f"{BASE_HEADER}full/{last_full.name}\n{sql}"
            _write_private(path, gzip.compress(body.encode("utf-8")))
            kind = f"증분 (기준 {last_full.name})"

    prune(dest, args.keep_days, now)
    print(f"[완료] {kind} 백업 {path} ({path.stat().st_size:,} bytes)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST, help=f"백업 위치 (기본 {DEFAULT_DEST})")
    ap.add_argument("--namespace", default="dr-tracker")
    ap.add_argument("--selector", default="app=dr-tracker")
    ap.add_argument("--full", action="store_true", help="강제 풀 백업")
    ap.add_argument("--full-weekday", type=int, default=6, help="풀 백업 요일 (월=0 ... 일=6, 기본 6)")
    ap.add_argument("--keep-days", type=int, default=14)
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("restore", help="백업 파일을 sqlite DB로 복원")
    rp.add_argument("backup", type=Path)
    rp.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    if args.cmd == "restore":
        restore(args.backup.resolve(), args.out)
    else:
        backup(args)


if __name__ == "__main__":
    main()
