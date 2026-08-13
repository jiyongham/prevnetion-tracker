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
):
    """일정 입력/수정 (변경 이력 기록)"""
    before = get_input(item_no, half)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO schedule_input
                (item_no, half, schedule, mode, is_done, evidence, note, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(item_no, half) DO UPDATE SET
                schedule   = excluded.schedule,
                mode       = excluded.mode,
                is_done    = excluded.is_done,
                evidence   = excluded.evidence,
                note       = excluded.note,
                updated_by = excluded.updated_by,
                updated_at = datetime('now', 'localtime')
        """, (item_no, half, schedule, mode, int(is_done), evidence, note, updated_by))

        # 변경 이력
        new_vals = {
            "schedule": schedule, "mode": mode,
            "is_done": str(int(is_done)), "evidence": evidence, "note": note,
        }
        for field, new_v in new_vals.items():
            old_v = str(before.get(field, "")) if before else ""
            if old_v != str(new_v):
                conn.execute("""
                    INSERT INTO change_log
                        (item_no, half, field, old_value, new_value, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                """, (item_no, half, field, old_v, str(new_v), updated_by))


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
