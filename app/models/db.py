# app/models/db.py
import json
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
            is_excluded INTEGER DEFAULT 0,   -- 관리자가 "제외" 버튼으로 처리 (증설 안 함 확정)
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

        CREATE TABLE IF NOT EXISTS eos_input (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL UNIQUE,   -- Insight Key (예: ASSET-00000)
            schedule    TEXT,
            is_done     INTEGER DEFAULT 0,
            evidence    TEXT,
            note        TEXT,
            owner       TEXT,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS eos_change_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL,
            field       TEXT,
            old_value   TEXT,
            new_value   TEXT,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS eos_remind_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ops_team        TEXT NOT NULL,
            recipient_name  TEXT,
            recipient_team  TEXT,
            ok              INTEGER DEFAULT 0,
            error           TEXT,
            sent_at         TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_eos_remind_log_team ON eos_remind_log(ops_team);

        CREATE TABLE IF NOT EXISTS eos_next_week_plan (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL,
            week_start  TEXT NOT NULL,   -- 'YYYY-MM-DD' (월요일) - JIRA/Confluence로 못 찾은 주의 수동 입력
            week_end    TEXT NOT NULL,   -- 'YYYY-MM-DD' (금요일)
            input_by    TEXT,
            input_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(item_no, week_start)
        );

        CREATE INDEX IF NOT EXISTS idx_eos_next_week_plan_week ON eos_next_week_plan(week_start);

        -- 금주 실적 수동 입력. 계획(eos_next_week_plan)과 같은 모양이지만 테이블을 나눈 이유:
        -- 한 대상이 '9/14 주 계획'으로 먼저 등록되고 그 주가 지나 '9/14 주 실적'으로도
        -- 등록되는 게 정상인데, 한 테이블에 두면 UNIQUE(item_no, week_start)에 걸려
        -- 나중 것이 앞의 것을 덮어써 버린다.
        CREATE TABLE IF NOT EXISTS eos_week_perf (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL,
            week_start  TEXT NOT NULL,   -- 'YYYY-MM-DD' (월요일)
            week_end    TEXT NOT NULL,   -- 'YYYY-MM-DD' (금요일)
            input_by    TEXT,
            input_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(item_no, week_start)
        );

        CREATE INDEX IF NOT EXISTS idx_eos_week_perf_week ON eos_week_perf(week_start);

        -- OS 커널 패치 대상의 웹 입력값. 이 엑셀엔 조치계획·완료 컬럼이 아예 없어서
        -- (자산 목록만 온다) 계획은 전적으로 화면에서 취합한다.
        CREATE TABLE IF NOT EXISTS kernel_input (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_no     TEXT NOT NULL UNIQUE,   -- Insight Key
            schedule    TEXT,
            is_done     INTEGER DEFAULT 0,
            evidence    TEXT,
            note        TEXT,
            owner       TEXT,
            is_excluded INTEGER DEFAULT 0,
            exclude_reason TEXT,
            updated_by  TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Polestar에서 '_OLD'를 확인한 관측 기록.
        -- 전환이 끝난 AS-IS 서버는 한동안 '_OLD'로 남아 있다가 결국 폐기(CI 삭제)된다.
        -- 판정은 매번 '지금 상태'를 다시 보기 때문에, 이 기록이 없으면 CI가 지워지는 순간
        -- 완료 근거가 사라져 완료 대수가 뒤로 간다. 그래서 판정 결과가 아니라 '관측 사실'을
        -- 날짜와 함께 남겨, 폐기 후에도 근거가 유지되게 한다.
        CREATE TABLE IF NOT EXISTS eos_polestar_seen (
            item_no    TEXT PRIMARY KEY,  -- Insight Key
            reason     TEXT DEFAULT '',   -- 어느 키로 확인했는지 ('CI명 매칭' / 'IP 경유 매칭 (...)')
            first_seen TEXT NOT NULL,     -- 'YYYY-MM-DD' 처음 확인한 날
            last_seen  TEXT NOT NULL      -- 'YYYY-MM-DD' 마지막으로 확인된 날 (이후로는 폐기 추정)
        );

        -- 리포트 발송 시점의 집계 스냅샷. 지난 발송분과 비교해 이상(완료 대수 감소 등)을
        -- 감지하는 데 쓴다. 이게 없으면 매 발송이 과거와 단절돼 "이 0대가 맞나"를 알 수 없다.
        CREATE TABLE IF NOT EXISTS report_snapshot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            domain      TEXT NOT NULL,     -- 'dr' | 'capacity' | 'eos_os' | 'eos_db'
            sent_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            total       INTEGER,
            done        INTEGER,
            rate        REAL,
            no_schedule INTEGER,
            perf_cnt    INTEGER,           -- 금주 실적 (없는 도메인은 NULL)
            plan_cnt    INTEGER            -- 차주 계획 (없는 도메인은 NULL)
        );

        CREATE INDEX IF NOT EXISTS idx_report_snapshot_domain ON report_snapshot(domain, sent_at);
        """)

        # 기존 DB에는 is_excluded 컬럼이 없을 수 있어 별도로 추가 (제외 버튼 기능용)
        cap_cols = {row["name"] for row in conn.execute("PRAGMA table_info(capacity_input)")}
        if "is_excluded" not in cap_cols:
            conn.execute("ALTER TABLE capacity_input ADD COLUMN is_excluded INTEGER DEFAULT 0")

        # 대상 구성(방식별/팀별 대수) 스냅샷. 총계만 봐서는 "실전환 -6, 무중단 +6"처럼
        # 합계는 그대로인데 구성만 바뀐 엑셀 변경을 놓친다.
        snap_cols = {row["name"] for row in conn.execute("PRAGMA table_info(report_snapshot)")}
        if "composition" not in snap_cols:
            conn.execute("ALTER TABLE report_snapshot ADD COLUMN composition TEXT")

        # 리마인드 종류(미기입/대략적 일정/사전 안내)별로 발송 이력을 따로 본다.
        # 같은 서비스라도 종류가 다르면 별개의 발송이라 한쪽을 보냈다고 다른 쪽까지
        # '발송함'으로 표시되면 안 된다. 기존 행은 전부 미기입 리마인드였다.
        # 제외 처리 시 관리자가 남기는 사유 (비고 note와 별개 - note는 일반 메모칸이라
        # 섞이면 왜 제외됐는지 나중에 구분이 안 된다)
        if "exclude_reason" not in cols:
            conn.execute("ALTER TABLE schedule_input ADD COLUMN exclude_reason TEXT")

        # EoS도 웹에서 제외 처리(+사유)를 할 수 있게 (엑셀의 'EOS 진행/제외' 컬럼과 별개)
        eos_cols = {row["name"] for row in conn.execute("PRAGMA table_info(eos_input)")}
        if "is_excluded" not in eos_cols:
            conn.execute("ALTER TABLE eos_input ADD COLUMN is_excluded INTEGER DEFAULT 0")
        if "exclude_reason" not in eos_cols:
            conn.execute("ALTER TABLE eos_input ADD COLUMN exclude_reason TEXT")

        rl_cols = {row["name"] for row in conn.execute("PRAGMA table_info(remind_log)")}
        if "kind" not in rl_cols:
            conn.execute("ALTER TABLE remind_log ADD COLUMN kind TEXT DEFAULT 'blank'")
            conn.execute("UPDATE remind_log SET kind = 'blank' WHERE kind IS NULL")


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
    exclude_reason: str | None = None,
):
    """
    일정 입력/수정 (변경 이력 기록).
    owner/exclude_reason은 명시적으로 넘겼을 때만 갱신한다 (None이면 기존 값 유지) —
    안 그러면 일반 일정 저장(대시보드)이 매번 담당자 수정이나 제외 사유를 지워버리게 된다.
    """
    before = get_input(item_no, half)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO schedule_input
                (item_no, half, schedule, mode, is_done, evidence, note, updated_by, owner,
                 exclude_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(item_no, half) DO UPDATE SET
                schedule       = excluded.schedule,
                mode           = excluded.mode,
                is_done        = excluded.is_done,
                evidence       = excluded.evidence,
                note           = excluded.note,
                updated_by     = excluded.updated_by,
                owner          = COALESCE(excluded.owner, schedule_input.owner),
                exclude_reason = COALESCE(excluded.exclude_reason, schedule_input.exclude_reason),
                updated_at     = datetime('now', 'localtime')
        """, (item_no, half, schedule, mode, int(is_done), evidence, note, updated_by, owner,
              exclude_reason))

        # 변경 이력
        new_vals = {
            "schedule": schedule, "mode": mode,
            "is_done": str(int(is_done)), "evidence": evidence, "note": note,
        }
        if owner is not None:
            new_vals["owner"] = owner
        if exclude_reason is not None:
            new_vals["exclude_reason"] = exclude_reason
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
    excluded: bool | None = None,
):
    """
    용량관리 일정 입력/수정 (변경 이력 기록).
    owner/excluded는 명시적으로 넘겼을 때만 갱신 (None이면 기존 값 유지) - 일반 행 저장(/api/capacity/save)이
    이 값들을 매번 안 넘기더라도 덮어써지지 않게 하기 위함.
    """
    before = get_capacity_input(item_no, sheet)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO capacity_input
                (item_no, sheet, schedule, is_done, evidence, note, updated_by, owner, is_excluded, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(item_no, sheet) DO UPDATE SET
                schedule    = excluded.schedule,
                is_done     = excluded.is_done,
                evidence    = excluded.evidence,
                note        = excluded.note,
                updated_by  = excluded.updated_by,
                owner       = COALESCE(excluded.owner, capacity_input.owner),
                is_excluded = COALESCE(excluded.is_excluded, capacity_input.is_excluded),
                updated_at  = datetime('now', 'localtime')
        """, (
            item_no, sheet, schedule, int(is_done), evidence, note, updated_by, owner,
            int(excluded) if excluded is not None else None,
        ))

        new_vals = {
            "schedule": schedule, "is_done": str(int(is_done)),
            "evidence": evidence, "note": note,
        }
        if owner is not None:
            new_vals["owner"] = owner
        if excluded is not None:
            new_vals["is_excluded"] = str(int(excluded))
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
    kind: str = "blank",
):
    """리마인드 DM 발송 시도 기록 (성공/실패 모두). kind: blank/hinted/upcoming"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO remind_log
                (half, service, kind, recipient_name, recipient_team, ok, error, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """, (half, service, kind, recipient_name, recipient_team, int(ok), error or ""))


def get_remind_log_summary(half: str, kind: str = "blank") -> dict[str, dict]:
    """
    해당 종류(kind)의 서비스별 가장 최근 발송 이력
    -> {service: {sent_at, ok, recipient_name, recipient_team, count}}
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT service, recipient_name, recipient_team, ok, sent_at
            FROM remind_log
            WHERE half = ? AND COALESCE(kind, 'blank') = ?
            ORDER BY sent_at ASC
        """, (half, kind)).fetchall()

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
    ops_team: str,
    recipient_name: str,
    recipient_team: str,
    ok: bool,
    error: str = "",
):
    """
    용량관리 미계획 리마인드 DM 발송 시도 기록 (성공/실패 모두).
    DATA/ARCH를 합쳐서 한 통으로 보내게 된 뒤로는 시트 구분이 의미 없어져서 "ALL"로 고정 기록한다
    (컬럼 자체는 과거 이력과의 호환을 위해 스키마 변경 없이 그대로 둠).
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO capacity_remind_log
                (sheet, ops_team, recipient_name, recipient_team, ok, error, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """, ("ALL", ops_team, recipient_name, recipient_team, int(ok), error or ""))


def get_capacity_remind_log_summary() -> dict[str, dict]:
    """
    운영팀별 가장 최근 발송 이력 -> {ops_team: {sent_at, ok, recipient_name, recipient_team, count}}.
    DATA/ARCH 통합 이후로는 시트 구분 없이 전체(과거 DATA/ARCH 개별 발송 이력 포함) 조회한다.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ops_team, recipient_name, recipient_team, ok, sent_at
            FROM capacity_remind_log ORDER BY sent_at ASC
        """).fetchall()

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


def get_eos_inputs() -> dict[str, dict]:
    """EoS 입력값 전체 조회 -> {item_no: row}"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM eos_input").fetchall()
    return {r["item_no"]: dict(r) for r in rows}


def get_eos_input(item_no: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM eos_input WHERE item_no = ?", (item_no,)
        ).fetchone()
    return dict(row) if row else None


def upsert_eos_input(
    item_no: str,
    schedule: str = "",
    is_done: bool = False,
    evidence: str = "",
    note: str = "",
    updated_by: str = "",
    owner: str | None = None,
    excluded: bool | None = None,
    exclude_reason: str | None = None,
):
    """
    EoS 조치계획 입력/수정 (변경 이력 기록).
    owner/excluded/exclude_reason은 명시적으로 넘겼을 때만 갱신 - 일반 행 저장이
    이 값들을 안 넘기더라도 덮어써지지 않게 한다.
    """
    before = get_eos_input(item_no)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO eos_input
                (item_no, schedule, is_done, evidence, note, updated_by, owner,
                 is_excluded, exclude_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(item_no) DO UPDATE SET
                schedule       = excluded.schedule,
                is_done        = excluded.is_done,
                evidence       = excluded.evidence,
                note           = excluded.note,
                updated_by     = excluded.updated_by,
                owner          = COALESCE(excluded.owner, eos_input.owner),
                is_excluded    = COALESCE(excluded.is_excluded, eos_input.is_excluded),
                exclude_reason = COALESCE(excluded.exclude_reason, eos_input.exclude_reason),
                updated_at     = datetime('now', 'localtime')
        """, (item_no, schedule, int(is_done), evidence, note, updated_by, owner,
              int(excluded) if excluded is not None else None, exclude_reason))

        new_vals = {
            "schedule": schedule, "is_done": str(int(is_done)),
            "evidence": evidence, "note": note,
        }
        if excluded is not None:
            new_vals["is_excluded"] = str(int(excluded))
        if exclude_reason is not None:
            new_vals["exclude_reason"] = exclude_reason
        if owner is not None:
            new_vals["owner"] = owner
        for field, new_v in new_vals.items():
            old_v = str(before.get(field, "")) if before else ""
            if old_v != str(new_v):
                conn.execute("""
                    INSERT INTO eos_change_log
                        (item_no, field, old_value, new_value, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))
                """, (item_no, field, old_v, str(new_v), updated_by))


def log_eos_remind(
    ops_team: str,
    recipient_name: str,
    recipient_team: str,
    ok: bool,
    error: str = "",
):
    """EoS 미계획 리마인드 DM 발송 시도 기록 (성공/실패 모두)"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO eos_remind_log
                (ops_team, recipient_name, recipient_team, ok, error, sent_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """, (ops_team, recipient_name, recipient_team, int(ok), error or ""))


# 주간 수동 입력 종류 -> 테이블. SQL에 테이블명을 직접 넣어야 해서(파라미터 바인딩 불가)
# 반드시 이 사전을 거쳐 이름을 얻는다.
_WEEK_TABLES = {"plan": "eos_next_week_plan", "perf": "eos_week_perf"}


def _week_table(kind: str) -> str:
    if kind not in _WEEK_TABLES:
        raise ValueError(f"kind는 {'/'.join(_WEEK_TABLES)} 중 하나여야 합니다: {kind}")
    return _WEEK_TABLES[kind]


def get_eos_next_week_plan(week_start: str, kind: str = "plan") -> dict[str, dict]:
    """특정 주(월요일 기준) 수동 입력된 대상 -> {item_no: row}. kind: 'plan'(차주 계획) | 'perf'(금주 실적)"""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM {_week_table(kind)} WHERE week_start = ?", (week_start,)
        ).fetchall()
    return {r["item_no"]: dict(r) for r in rows}


def add_eos_next_week_plan(
    item_nos: list[str], week_start: str, week_end: str, input_by: str, kind: str = "plan"
):
    """챗봇에서 확인된 항목들을 그 주 대상으로 추가 (이미 있으면 입력자/시각만 갱신)."""
    table = _week_table(kind)
    with get_conn() as conn:
        for item_no in item_nos:
            conn.execute(f"""
                INSERT INTO {table} (item_no, week_start, week_end, input_by, input_at)
                VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(item_no, week_start) DO UPDATE SET
                    input_by = excluded.input_by,
                    input_at = datetime('now', 'localtime')
            """, (item_no, week_start, week_end, input_by))


def remove_eos_next_week_plan(item_no: str, week_start: str, kind: str = "plan"):
    """잘못 추가된 항목 제거"""
    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM {_week_table(kind)} WHERE item_no = ? AND week_start = ?",
            (item_no, week_start),
        )


def get_eos_remind_log_summary() -> dict[str, dict]:
    """운영팀별 가장 최근 발송 이력 -> {ops_team: {sent_at, ok, recipient_name, recipient_team, count}}"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ops_team, recipient_name, recipient_team, ok, sent_at
            FROM eos_remind_log ORDER BY sent_at ASC
        """).fetchall()

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


def get_last_report_snapshot(domain: str) -> dict | None:
    """해당 도메인의 가장 최근 발송 스냅샷 (없으면 None - 첫 발송)"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_snapshot WHERE domain = ? ORDER BY sent_at DESC, id DESC LIMIT 1",
            (domain,),
        ).fetchone()
    if not row:
        return None
    snap = dict(row)
    try:
        snap["composition"] = json.loads(snap["composition"]) if snap.get("composition") else None
    except (TypeError, ValueError):
        snap["composition"] = None
    return snap


def save_report_snapshot(domain: str, metrics: dict):
    """발송 시점 집계 저장 (다음 발송 때 비교 기준이 된다)"""
    with get_conn() as conn:
        comp = metrics.get("composition")
        conn.execute("""
            INSERT INTO report_snapshot
                (domain, sent_at, total, done, rate, no_schedule, perf_cnt, plan_cnt, composition)
            VALUES (?, datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?, ?)
        """, (
            domain,
            metrics.get("total"), metrics.get("done"), metrics.get("rate"),
            metrics.get("no_schedule"), metrics.get("perf_cnt"), metrics.get("plan_cnt"),
            json.dumps(comp, ensure_ascii=False) if comp else None,
        ))


# ─────────────────────────────────────────────
# Polestar '_OLD' 관측 기록 (완료 근거 보존)
# ─────────────────────────────────────────────
def get_eos_polestar_seen() -> dict[str, dict]:
    """{item_no: {reason, first_seen, last_seen}}"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM eos_polestar_seen").fetchall()
    return {r["item_no"]: dict(r) for r in rows}


def record_eos_polestar_seen(reasons: dict[str, str]) -> int:
    """
    이번 조회에서 '_OLD'로 확인된 대상을 기록. 반환: 새로 추가된 건수.
    first_seen은 처음 값을 유지하고 last_seen만 갱신한다 - 언제부터 전환돼 있었는지가
    실적 집계의 근거이므로 나중 조회로 덮어쓰면 안 된다.
    """
    if not reasons:
        return 0
    with get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM eos_polestar_seen").fetchone()["c"]
        conn.executemany("""
            INSERT INTO eos_polestar_seen (item_no, reason, first_seen, last_seen)
            VALUES (?, ?, date('now', 'localtime'), date('now', 'localtime'))
            ON CONFLICT(item_no) DO UPDATE SET
                reason    = excluded.reason,
                last_seen = excluded.last_seen
        """, [(no, reason) for no, reason in reasons.items()])
        after = conn.execute("SELECT COUNT(*) c FROM eos_polestar_seen").fetchone()["c"]
    return after - before


def delete_eos_polestar_seen(item_no: str) -> None:
    """오탐으로 판단된 관측 기록 삭제 (관리자용). 다음 조회에서 다시 보이면 또 기록된다."""
    with get_conn() as conn:
        conn.execute("DELETE FROM eos_polestar_seen WHERE item_no = ?", (item_no,))


# ─────────────────────────────────────────────
# OS 커널 패치 입력값 (EoS와 같은 구조)
# ─────────────────────────────────────────────
def get_kernel_inputs() -> dict[str, dict]:
    """{item_no: row} 전체"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM kernel_input").fetchall()
    return {r["item_no"]: dict(r) for r in rows}


def get_kernel_input(item_no: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM kernel_input WHERE item_no = ?", (item_no,)).fetchone()
    return dict(row) if row else None


def upsert_kernel_input(
    item_no: str,
    schedule: str = "",
    is_done: bool = False,
    evidence: str = "",
    note: str = "",
    updated_by: str = "",
    owner: str | None = None,
    is_excluded: bool | None = None,
    exclude_reason: str | None = None,
):
    """
    웹 입력 저장. owner/is_excluded/exclude_reason 은 None이면 기존 값을 유지한다
    (담당자 수정과 제외 처리가 서로의 값을 지우지 않도록).
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO kernel_input
                (item_no, schedule, is_done, evidence, note, owner,
                 is_excluded, exclude_reason, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(item_no) DO UPDATE SET
                schedule       = excluded.schedule,
                is_done        = excluded.is_done,
                evidence       = excluded.evidence,
                note           = excluded.note,
                owner          = COALESCE(excluded.owner, kernel_input.owner),
                is_excluded    = COALESCE(excluded.is_excluded, kernel_input.is_excluded),
                exclude_reason = COALESCE(excluded.exclude_reason, kernel_input.exclude_reason),
                updated_by     = excluded.updated_by,
                updated_at     = datetime('now', 'localtime')
        """, (
            item_no, schedule, int(is_done), evidence, note, owner,
            None if is_excluded is None else int(is_excluded),
            exclude_reason, updated_by,
        ))
