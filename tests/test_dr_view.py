from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.services.dr_view import action_counts, decorate_details, recently_changed
from app.web.deps import templates


def test_action_counts_use_actual_rows():
    rows = [
        {"evidence": "", "schedule_raw": "", "completed": False},
        {"evidence": "TEST-1", "schedule_raw": "1/1", "status_label": "지연", "evidence_level": "bad"},
        {"evidence": "TEST-2", "completed": True},
    ]
    decorate_details(rows, "Asia/Seoul")
    counts = action_counts(rows)
    assert counts == {"all": 3, "evidence": 1, "schedule": 1, "overdue": 1, "warning": 1, "recent": 0}
    assert len([r for r in rows if r["actions"]["warning"]]) == 1


@pytest.mark.parametrize("days, expected", [(0, True), (7, True), (8, False), (-1, False)])
def test_recent_change_window(days, expected):
    now = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("Asia/Seoul"))
    assert recently_changed((now - timedelta(days=days)).isoformat(), now) is expected


def test_recent_change_invalid_and_local_time():
    now = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("Asia/Seoul"))
    assert not recently_changed("invalid", now)
    assert not recently_changed(None, now)
    assert recently_changed("2026-10-12 11:00:00", now)


@pytest.mark.parametrize("changer, visible", [("관리자", False), ("담당자", True)])
def test_row_hides_admin_but_preserves_source(changer, visible):
    # 매크로만 렌더링하므로 운영 엑셀/DB/네트워크가 필요 없다.
    row = dict(no="1", system_name="테스트", hostname="host", ops_team="팀", owner="담당",
               mode="실전환", input_source="web", updated_by=changer, updated_at="2026-10-12",
               completed=False, schedule_disp="", evidence="", note="", jira_key="",
               status_label="미계획", recent_change=False)
    source, _, _ = templates.env.loader.get_source(templates.env, "dashboard.html")
    macro = source[source.index("{% macro detail_row(d) %}"):source.index("{% endmacro %}") + len("{% endmacro %}")]
    module = templates.env.from_string(macro).make_module({"admins": ["관리자"], "compare": False})
    html = str(module.detail_row(row))
    assert (changer in html) is visible
    assert row["updated_by"] == changer


def test_reminder_label_is_valid_korean():
    source, _, _ = templates.env.loader.get_source(templates.env, "remind_preview.html")
    assert "완전 미기입" in source
    assert "폈 미기입" not in source


def test_decorate_details_with_half_uses_change_history(monkeypatch):
    """half를 주면 단순 bool이 아니라 change_history 기반 상세 dict를 붙인다."""
    from app.config import settings
    from app.models.db import upsert_input

    monkeypatch.setattr(settings, "admin_users", "관리자A")
    upsert_input(
        item_no="1", half="H2", schedule="12/25", mode="실전환",
        is_done=False, evidence="", note="", updated_by="관리자A",
    )
    rows = [{"no": "1", "evidence": "", "schedule_raw": "12/25", "completed": False}]
    decorate_details(rows, "Asia/Seoul", half="H2")

    assert rows[0]["actions"]["recent"] is True
    change = rows[0]["recent_change"]
    assert isinstance(change, dict)
    # 관리자가 바꾸 건이라도 표시용 라벨은 가려져야 한다.
    assert change["by_display"] == "관리자"


def test_decorate_details_without_half_falls_back_to_bool():
    """half 없이 호출하면(기존 호출부) 단순 bool 판정으로 폴백한다 - 하위 호환성."""
    rows = [{"evidence": "", "schedule_raw": "", "completed": False, "updated_at": ""}]
    decorate_details(rows, "Asia/Seoul")
    assert rows[0]["recent_change"] is False
    assert rows[0]["actions"]["recent"] is False


def test_detail_row_macro_renders_dict_recent_change():
    """recent_change가 dict(상세 요약)일 때도 매크로 렌더링 실패 없이 요약/변경자 라벨을 보여준다."""
    row = dict(no="1", system_name="테스트", hostname="host", ops_team="팀", owner="담당",
               mode="실전환", input_source="web", updated_by="김담당", updated_at="2026-10-12",
               completed=False, schedule_disp="", evidence="", note="", jira_key="",
               status_label="미계획",
               recent_change={"summary": "일정 변경", "by_display": "김담당", "at": "2026-10-10 10:00:00"})
    source, _, _ = templates.env.loader.get_source(templates.env, "dashboard.html")
    macro = source[source.index("{% macro detail_row(d) %}"):source.index("{% endmacro %}") + len("{% endmacro %}")]
    module = templates.env.from_string(macro).make_module({"admins": ["관리자A"], "compare": False})
    html = str(module.detail_row(row))
    assert "일정 변경" in html
    assert "김담당" in html
