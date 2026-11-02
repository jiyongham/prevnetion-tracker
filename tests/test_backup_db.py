# tests/test_backup_db.py
"""로컬 백업 스크립트 (scripts/backup_db.py) - 차등 SQL·복원·보관 정리. kubectl은 쓰지 않는다."""
import gzip
import sqlite3
from contextlib import closing
from datetime import datetime

from scripts import backup_db

SCHEMA = """
CREATE TABLE schedule_input (id INTEGER PRIMARY KEY AUTOINCREMENT, item_no TEXT, note TEXT, UNIQUE(item_no));
CREATE TABLE eos_polestar_seen (item_no TEXT PRIMARY KEY, reason TEXT);
"""


def _db(path, statements: str):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA + statements)
        conn.commit()
    return path


def test_차등_SQL을_적용하면_원본과_같아진다(tmp_path):
    base = _db(tmp_path / "base.db", """
        INSERT INTO schedule_input (item_no, note) VALUES ('1', 'a'), ('2', 'b'), ('3', 'c');
        INSERT INTO eos_polestar_seen VALUES ('ASSET-1', 'x'), ('ASSET-2', 'y');
    """)
    current = _db(tmp_path / "current.db", """
        INSERT INTO schedule_input (item_no, note) VALUES ('1', 'a'), ('2', 'b'), ('3', 'c');
        INSERT INTO eos_polestar_seen VALUES ('ASSET-1', 'x'), ('ASSET-2', 'y');
        UPDATE schedule_input SET note = 'it''s 변경' WHERE item_no = '2';
        DELETE FROM schedule_input WHERE item_no = '3';
        INSERT INTO schedule_input (item_no, note) VALUES ('4', NULL);
        INSERT OR REPLACE INTO eos_polestar_seen VALUES ('ASSET-1', 'z');
    """)

    sql = backup_db.make_diff(base, current)
    with closing(sqlite3.connect(base)) as conn:
        conn.executescript(sql)

    assert backup_db._content_hash(base) == backup_db._content_hash(current)
    with closing(sqlite3.connect(base)) as conn:
        # AUTOINCREMENT 카운터까지 맞아야 복원 후 새 행이 지워진 id를 재사용하지 않는다
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='schedule_input'").fetchone()[0] == 4


def test_스키마가_다르면_차등_대신_None(tmp_path):
    base = _db(tmp_path / "base.db", "")
    current = _db(tmp_path / "current.db", "ALTER TABLE schedule_input ADD COLUMN owner TEXT;")
    assert backup_db.make_diff(base, current) is None


def test_증분_복원은_기준_풀백업에_SQL을_적용한다(tmp_path):
    dest = tmp_path / "backups"
    (dest / "full").mkdir(parents=True)
    (dest / "diff").mkdir()
    base = _db(tmp_path / "base.db", "INSERT INTO schedule_input (item_no, note) VALUES ('1', 'a');")
    current = _db(tmp_path / "current.db", "INSERT INTO schedule_input (item_no, note) VALUES ('1', 'b');")

    full = dest / "full" / "tracker-20261025-0300.db.gz"
    full.write_bytes(gzip.compress(base.read_bytes()))
    diff = dest / "diff" / "tracker-20261027-0300.sql.gz"
    diff.write_bytes(gzip.compress(
        f"{backup_db.BASE_HEADER}full/{full.name}\n{backup_db.make_diff(base, current)}".encode()
    ))

    out = tmp_path / "restored.db"
    backup_db.restore(diff, out)
    assert backup_db._content_hash(out) == backup_db._content_hash(current)


def test_보관_정리는_남은_증분의_기준_풀백업을_지우지_않는다(tmp_path):
    dest = tmp_path / "backups"
    (dest / "full").mkdir(parents=True)
    (dest / "diff").mkdir()
    old_full = dest / "full" / "tracker-20261004-0300.db.gz"     # 25일 전, 기준 아님 -> 삭제
    base_full = dest / "full" / "tracker-20261011-0300.db.gz"    # 18일 전이지만 남은 증분의 기준 -> 유지
    new_full = dest / "full" / "tracker-20261025-0300.db.gz"
    for f in (old_full, base_full, new_full):
        f.write_bytes(gzip.compress(b""))
    old_diff = dest / "diff" / "tracker-20261012-0300.sql.gz"    # 17일 전 -> 삭제
    kept_diff = dest / "diff" / "tracker-20261016-0300.sql.gz"   # 13일 전 -> 유지
    for d in (old_diff, kept_diff):
        d.write_bytes(gzip.compress(f"{backup_db.BASE_HEADER}full/{base_full.name}\n".encode()))

    backup_db.prune(dest, keep_days=14, now=datetime(2026, 10, 29, 3, 0))

    assert not old_full.exists() and not old_diff.exists()
    assert base_full.exists() and new_full.exists() and kept_diff.exists()
