# app/models/db.py
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

DB_PATH = Path(settings.db_path)


def init_db():
    """테이블 생성"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS schedule_input (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL,
            half        TEXT NOT NULL,
            schedule    TEXT,
            mode        TEXT,
            is_done     INTEGER DEFAULT 0,
            evidence    TEXT,
            note        TEXT,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(item_no, half)
        );

        CREATE TABLE IF NOT EXISTS change_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL,
            half        TEXT NOT NULL,
            field       TEXT,
            old_value   TEXT,
            new_value   TEXT,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_schedule_half ON schedule_input(half);
        """)
        # 기존 DB에는 owner 컬럼이 없을 수 있어 별도로 추가 (담당자 수정 기능용)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(schedule_input)")}
        if "owner" not in cols:
            conn.execute("ALTER TABLE schedule_input ADD COLUMN owner TEXT")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS remind_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            half            TEXT NOT NULL,
            service         TEXT NOT NULL,
            recipient_name  TEXT,
            recipient_team  TEXT,
            ok              INTEGER DEFAULT 0,
            error           TEXT,
            sent_at         TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_remind_log_service ON remind_log(half, service);

        CREATE TABLE IF NOT EXISTS capacity_input (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL,   -- 'DATA:3' / 'ARCH:7' 형식 (시트+NO)
            sheet       TEXT NOT NULL,
            schedule    TEXT,
            is_done     INTEGER DEFAULT 0,
            evidence    TEXT,
            note        TEXT,
            owner       TEXT,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(item_no, sheet)
        );

        CREATE TABLE IF NOT EXISTS capacity_change_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL,
            sheet       TEXT NOT NULL,
            field       TEXT,
            old_value   TEXT,
            new_value   TEXT,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_capacity_input_sheet ON capacity_input(sheet);

        CREATE TABLE IF NOT EXISTS capacity_remind_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet           TEXT NOT NULL,
            ops_team        TEXT NOT NULL,
            recipient_name  TEXT,
            recipient_team  TEXT,
            ok              INTEGER DEFAULT 0,
            error           TEXT,
            sent_at         TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_capacity_remind_log_team ON capacity_remind_log(sheet, ops_team);
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_inputs(half: str) -> dict[str, dict]:
    """반기별 입력값 전체 조회 -> {item_no: row}"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM schedule_input WHERE half = ?", (half,)
        ).fetchall()
    return {r["item_no"]: dict(r) for r in rows}


def get_input(item_no: str, half: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM schedule_input WHERE item_no = ? AND half = ?",
            (item_no, half),
        ).fetchone()
    return dict(row) if row else None


def upsert_input(
    item_no: str,
    half: str,
    schedule: str = "",
    mode: str = "",
    is_done: bool = False,
    evidence: str = "",
    note: str = "",
    updated_by: str = "",
    owner: str | None = None,
):
    """
    일정 입력/수정 (변경 이력 기록).
    owner는 명시적으로 넘겼을 때만 갱신한다 (None이면 기존 담당자 override 유지) —
    안 그러면 일반 일정 저장(대시보드)이 매번 담당자 수정 내용을 지워버리게 된다.
    """
    before = get_input(item_no, half)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO schedule_input
                (item_no, half, schedule, mode, is_done, evidence, note, updated_by, owner, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(item_no, half) DO UPDATE SET
                schedule   = excluded.schedule,
                mode       = excluded.mode,
                is_done    = excluded.is_done,
                evidence   = excluded.evidence,
                note       = excluded.note,
                updated_by = excluded.updated_by,
                owner      = COALESCE(excluded.owner, schedule_input.owner),
                updated_at = datetime('now', 'localtime')
        """, (item_no, half, schedule, mode, int(is_done), evidence, note, updated_by, owner))

        # 변경 이력
        new_vals = {
            "schedule": schedule, "mode": mode,
            "is_done": str(int(is_done)), "evidence": evidence, "note": note,
        }
        if owner is not None:
            new_vals["owner"] = owner
        for field, new_v in new_vals.items():
            old_v = str(before.get(field, "")) if before else ""
            if old_v != str(new_v):
                conn.execute("""
                    INSERT INTO change_log
                        (item_no, half, field, old_value, new_value, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                """, (item_no, half, field, old_v, str(new_v), updated_by))


def get_capacity_inputs(sheet: str) -> dict[str, dict]:
    """시트별(DATA/ARCH) 입력값 전체 조회 -> {item_no: row}"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM capacity_input WHERE sheet = ?", (sheet,)
        ).fetchall()
    return {r["item_no"]: dict(r) for r in rows}


def get_capacity_input(item_no: str, sheet: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM capacity_input WHERE item_no = ? AND sheet = ?",
            (item_no, sheet),
        ).fetchone()
    return dict(row) if row else None


def upsert_capacity_input(
    item_no: str,
    sheet: str,
    schedule: str = "",
    is_done: bool = False,
    evidence: str = "",
    note: str = "",
    updated_by: str = "",
    owner: str | None = None,
):
    """용량관리 일정 입력/수정 (변경 이력 기록). owner는 명시적으로 넘겼을 때만 갱신."""
    before = get_capacity_input(item_no, sheet)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO capacity_input
                (item_no, sheet, schedule, is_done, evidence, note, updated_by, owner, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(item_no, sheet) DO UPDATE SET
                schedule   = excluded.schedule,
                is_done    = excluded.is_done,
                evidence   = excluded.evidence,
                note       = excluded.note,
                updated_by = excluded.updated_by,
                owner      = COALESCE(excluded.owner, capacity_input.owner),
                updated_at = datetime('now', 'localtime')
        """, (item_no, sheet, schedule, int(is_done), evidence, note, updated_by, owner))

        new_vals = {
            "schedule": schedule, "is_done": str(int(is_done)),
            "evidence": evidence, "note": note,
        }
        if owner is not None:
            new_vals["owner"] = owner
        for field, new_v in new_vals.items():
            old_v = str(before.get(field, "")) if before else ""
            if old_v != str(new_v):
                conn.execute("""
                    INSERT INTO capacity_change_log
                        (item_no, sheet, field, old_value, new_value, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                """, (item_no, sheet, field, old_v, str(new_v), updated_by))


def get_logs(item_no: str | None = None, limit: int = 50) -> list[dict]:
    """변경 이력 조회"""
    with get_conn() as conn:
        if item_no:
            rows = conn.execute(
                "SELECT * FROM change_log WHERE item_no = ? ORDER BY id DESC LIMIT ?",
                (item_no, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def log_remind(
    half: str,
    service: str,
    recipient_name: str,
    recipient_team: str,
    ok: bool,
    error: str = "",
):
    """미계획 리마인드 DM 발송 시도 기록 (성공/실패 모두)"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO remind_log
                (half, service, recipient_name, recipient_team, ok, error, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """, (half, service, recipient_name, recipient_team, int(ok), error or ""))


def get_remind_log_summary(half: str) -> dict[str, dict]:
    """서비스별 가장 최근 발송 이력 -> {service: {sent_at, ok, recipient_name, recipient_team, count}}"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT service, recipient_name, recipient_team, ok, sent_at
            FROM remind_log WHERE half = ? ORDER BY sent_at ASC
        """, (half,)).fetchall()

    summary: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        s = summary.setdefault(d["service"], {"count": 0})
        s["count"] += 1
        s["sent_at"] = d["sent_at"]
        s["ok"] = bool(d["ok"])
        s["recipient_name"] = d["recipient_name"]
        s["recipient_team"] = d["recipient_team"]
    return summary


def log_capacity_remind(
    sheet: str,
    ops_team: str,
    recipient_name: str,
    recipient_team: str,
    ok: bool,
    error: str = "",
):
    """용량관리 미계획 리마인드 DM 발송 시도 기록 (성공/실패 모두)"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO capacity_remind_log
                (sheet, ops_team, recipient_name, recipient_team, ok, error, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """, (sheet, ops_team, recipient_name, recipient_team, int(ok), error or ""))


def get_capacity_remind_log_summary(sheet: str) -> dict[str, dict]:
    """운영팀별 가장 최근 발송 이력 -> {ops_team: {sent_at, ok, recipient_name, recipient_team, count}}"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ops_team, recipient_name, recipient_team, ok, sent_at
            FROM capacity_remind_log WHERE sheet = ? ORDER BY sent_at ASC
        """, (sheet,)).fetchall()

    summary: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        s = summary.setdefault(d["ops_team"], {"count": 0})
        s["count"] += 1
        s["sent_at"] = d["sent_at"]
        s["ok"] = bool(d["ok"])
        s["recipient_name"] = d["recipient_name"]
        s["recipient_team"] = d["recipient_team"]
    return summary
