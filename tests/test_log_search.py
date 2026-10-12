from app.models.db import get_conn, search_logs


def seed(count=1):
    with get_conn() as conn:
        for index in range(count):
            conn.execute(
                "INSERT INTO change_log (item_no, half, field, old_value, new_value, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(index), "H1" if index == 0 else "H2", "evidence", "old", "special" if index == 0 else "new", "비공개관리자", "2026-10-12 10:00:00"),
            )


def test_search_before_limit_and_hostname():
    seed(205)
    rows, total, page = search_logs(q="special")
    assert total == 1 and rows[0]["item_no"] == "0"
    rows, total, _ = search_logs(q="SERVER", hostnames={"0": "server-01"})
    assert total == 1 and rows[0]["item_no"] == "0"


def test_search_filters_and_pagination():
    seed(105)
    rows, total, page = search_logs(half="H2", page=2)
    assert total == 104 and page == 2 and len(rows) == 50
    rows, total, page = search_logs(half="H2", page=999)
    assert page == 3 and len(rows) == 4
    assert search_logs(q="special", half="H2")[1] == 0
    assert search_logs(item_no="0", q="증적", field_labels={"evidence": "증적"})[1] == 1


def test_search_literal_and_admin_masking():
    seed()
    assert search_logs(q="%") [1] == 0
    assert search_logs(q="' OR 1=1 --")[1] == 0
    assert search_logs(q="비공개", admin_names={"비공개관리자"})[1] == 0
    assert search_logs(q="관리자", admin_names={"비공개관리자"})[1] == 1
