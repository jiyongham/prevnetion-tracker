from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.services.dr_view import action_counts, decorate_details, recently_changed
from app.web.deps import templates
from app.services.dr_view import select_details


@pytest.mark.parametrize("status,action,expected,normalized", [
    ("done", "all", ["1"], ("done", "all")),
    ("done", "evidence", ["2"], (None, "evidence")),
    (None, "overdue", ["2"], ("overdue", "all")),
    ("invalid", "invalid", ["1", "2"], (None, "all")),
])
def test_select_details_preserves_counts_and_precedence(status, action, expected, normalized):
    from copy import deepcopy
    rows = [
        {"no": "1", "completed": True, "status_label": "완료", "evidence": "TEST-1"},
        {"no": "2", "completed": False, "status_label": "지연", "evidence": ""},
    ]
    decorate_details(rows, "Asia/Seoul")
    original = deepcopy(rows)
    selected, new_status, new_action, counts, statuses = select_details(rows, status, action)
    assert [row["no"] for row in selected] == expected
    assert (new_status, new_action) == normalized
    assert counts["all"] == 2 and statuses["done"] == 1 and statuses["pending"] == 1
    assert rows == original


def test_select_details_empty():
    selected, status, action, counts, statuses = select_details([], None, "all")
    assert selected == [] and status is None and action == "all"
    assert all(value == 0 for value in (*counts.values(), *statuses.values()))


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


def _status_row(no, team, label, schedule=None, completed=False, service="서비스A", company="관계사A", **extra):
    from datetime import date
    return {"no": no, "ops_team": team, "status_label": label, "completed": completed,
            "schedule": date.fromisoformat(schedule) if schedule else None,
            "business_name": service, "company": company, "system_name": f"SYS{no}", **extra}


def test_status_summary_상태건수와_대표날짜():
    from datetime import date
    from app.services.dr_view import status_summary

    rows = [
        _status_row("1", "A팀", "완료", completed=True),
        _status_row("2", "A팀", "예정", "2026-11-10"),
        _status_row("3", "A팀", "예정", "2026-11-05"),
        _status_row("4", "A팀", "지연", "2026-10-20"),
        _status_row("5", "A팀", "지연", "2026-10-28"),
        _status_row("6", "A팀", "미계획", service="서비스B"),
        _status_row("7", "A팀", "대략", schedule_raw="X"),   # 관리자 제외 - 상태 건수에서 뺀다
    ]
    [a] = status_summary(rows, lambda d: d["ops_team"], date(2026, 11, 2),
                         last_remind={"서비스B": "2026-10-30 09:00:00"}, excluded_nos={"7"})

    assert (a["done"], a["total"]) == (1, 7)
    assert (a["scheduled"], a["overdue"], a["approximate"], a["unplanned"]) == (2, 2, 0, 1)
    assert a["next_date"] == date(2026, 11, 5) and a["next_in_days"] == 3
    assert a["oldest_overdue"] == date(2026, 10, 20) and a["overdue_days"] == 13
    assert a["last_remind"] == "2026-10-30 09:00:00"
    assert a["action"] == 3


def test_status_summary_조치가_필요한_순으로_정렬():
    from datetime import date
    from app.services.dr_view import status_summary

    rows = [
        # B팀: 완료율은 낮지만(0%) 전부 예정 -> 조치 필요 0
        _status_row("1", "B팀", "예정", "2026-11-10"),
        # A팀: 완료율 50%지만 지연 1 -> 먼저
        _status_row("2", "A팀", "완료", completed=True),
        _status_row("3", "A팀", "지연", "2026-10-20"),
    ]
    result = status_summary(rows, lambda d: d["ops_team"], date(2026, 11, 2))
    assert [g["key"] for g in result] == ["A팀", "B팀"]


def test_build_status_tabs_서비스는_관계사별로_나누고_링크를_인코딩():
    from datetime import date
    from app.web.routes.dr import build_status_tabs

    rows = [
        _status_row("1", "A&B 팀", "미계획", company="관계사1", service="공통"),
        _status_row("2", "A&B 팀", "미계획", company="관계사2", service="공통"),
    ]
    tabs = build_status_tabs(rows, "H2", None, date(2026, 11, 2))

    services = {(r["sublabel"], r["label"]) for r in tabs["service"]}
    assert services == {("관계사1", "공통"), ("관계사2", "공통")}
    [team] = tabs["team"]
    assert team["url"] == "/dr?half=H2&team=A%26B+%ED%8C%80"


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
