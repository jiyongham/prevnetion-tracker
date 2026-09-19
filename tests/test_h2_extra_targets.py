# tests/test_h2_extra_targets.py
"""
DR훈련 하반기(H2) 화면에 상반기(H1) '실전환' 대상을 참고용으로 같이 노출하는 기능의
회귀 테스트.

배경: H2 대시보드의 통계(완료율/by_team/by_company/리포트)는 상반기 무중단 대상
173대로만 한정된다(scope_h2_targets, dr_data.load_items). 그런데 하반기 엑셀
시트의 수행방식 컬럼을 보면 이 173대 중 다수가 '실전환'으로 다시 기재돼 있고,
반대로 상반기 실전환이었던 102대는 하반기 통계에 아예 안 잡힌다. 담당자들이
이 102대의 일정/증적도 하반기 화면에서 같이 입력하고 싶어해서, 통계에는
영향을 주지 않으면서 "참고용 목록"으로만 노출하는 기능을 추가했다.

핵심 불변 조건(이 테스트가 지키려는 것):
1. get_h1_real_target_nos()는 H1에서 '실전환'이었던 대상만 정확히 골라낸다.
2. load_extra_h2_items()가 반환하는 항목들은 173대(H1 무중단) 대상과 겹치지 않고,
   각 항목에 is_extra=True 플래그가 붙는다.
3. /dr 라우트가 half=H2일 때 extra_details를 500 에러 없이 응답에 반영한다.
"""
import pandas as pd
import pytest

from app.core import excel_loader
from app.core.excel_loader import (
    get_h1_nonstop_target_nos,
    get_h1_real_target_nos,
    get_targets,
    load_dr_items_merged,
)
from app.services import dr_data


def _make_dr_excel(path):
    """
    H1 대상 4건: 무중단 2건(NO 1,2) + 실전환 2건(NO 3,4).
    NO 3,4는 하반기 시트에도 값이 있지만(담당자가 재기재), get_h1_real_target_nos는
    'H1' 컬럼 기준으로만 판단하므로 H2 시트의 mode 표기와 무관하게 골라져야 한다.
    """
    rows = [
        {
            "시스템명": "무중단시스템1", "NO": "1", "관계사": "관계사A", "주업무명": "업무A",
            "호스트명": "host-01", "IP": "10.0.0.1", "관리파트": "파트A",
            "APP운영팀": "운영팀A", "담당자": "홍길동", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-01", "실 전환 / 무중단": "무중단", "상반기 완료": "O",
            "하반기 일정": "2026-09-01", "실 전환 / 무중단.1": "무중단", "하반기 완료": "",
        },
        {
            "시스템명": "무중단시스템2", "NO": "2", "관계사": "관계사A", "주업무명": "업무A",
            "호스트명": "host-02", "IP": "10.0.0.2", "관리파트": "파트A",
            "APP운영팀": "운영팀A", "담당자": "홍길동", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-02", "실 전환 / 무중단": "무중단", "상반기 완료": "O",
            "하반기 일정": "2026-09-02", "실 전환 / 무중단.1": "무중단", "하반기 완료": "",
        },
        {
            "시스템명": "실전환시스템1", "NO": "3", "관계사": "관계사B", "주업무명": "업무B",
            "호스트명": "host-03", "IP": "10.0.0.3", "관리파트": "파트B",
            "APP운영팀": "운영팀B", "담당자": "김철수", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-03", "실 전환 / 무중단": "실전환", "상반기 완료": "O",
            # 담당자가 하반기 시트에 방식을 다시 기재하면서 표기가 바뀐 경우를 재현
            "하반기 일정": "2026-09-03", "실 전환 / 무중단.1": "실전환", "하반기 완료": "",
        },
        {
            "시스템명": "실전환시스템2", "NO": "4", "관계사": "관계사B", "주업무명": "업무B",
            "호스트명": "host-04", "IP": "10.0.0.4", "관리파트": "파트B",
            "APP운영팀": "운영팀B", "담당자": "김철수", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-04", "실 전환 / 무중단": "실 전환", "상반기 완료": "O",
            "하반기 일정": "2026-09-04", "실 전환 / 무중단.1": "무중단", "하반기 완료": "",
        },
    ]
    df = pd.DataFrame(rows)
    df.to_excel(path, index=False)
    return path

@pytest.fixture()
def dr_excel_path(tmp_path, monkeypatch):
    """
    settings.excel_path를 테스트용 엑셀로 교체한다. get_h1_nonstop_target_nos/
    get_h1_real_target_nos/scope_h2_targets/load_dr_items는 모두 excel_path 인자가
    None이면 settings.excel_path를 기본값으로 쓰므로, 개별 함수를 각각 모킹패치하는 대신
    이 하나만 바꾸면 dr_data 내부의 모든 호출경로(load_items 포함)가
    일관되게 테스트 데이터를 보게 된다.
    """
    path = tmp_path / "targets.xlsx"
    _make_dr_excel(path)
    monkeypatch.setattr(excel_loader.settings, "excel_path", str(path))
    excel_loader._items_cache.clear()  # 이전 테스트의 경로/mtime 캠시가 남아있지 않게
    return path


# ─────────────────────────────────────────────
# get_h1_real_target_nos()
# ─────────────────────────────────────────────

def test_get_h1_real_target_nos_returns_only_real_mode(dr_excel_path):
    """H1 '실 전환 / 무중단' 컬럼에 실전환(공백 유무 무관)이 적힌 대상만 반환."""
    real_nos = excel_loader.get_h1_real_target_nos(excel_path=str(dr_excel_path))
    assert real_nos == {"3", "4"}


def test_get_h1_real_target_nos_disjoint_from_nonstop(dr_excel_path):
    """실전환 집합과 무중단 집합은 서로 겹치지 않는다 (같은 대상이 두 통계에 동시에 안 잡혀야 함)."""
    real_nos = excel_loader.get_h1_real_target_nos(excel_path=str(dr_excel_path))
    nonstop_nos = get_h1_nonstop_target_nos(excel_path=str(dr_excel_path))
    assert real_nos == {"3", "4"}
    assert nonstop_nos == {"1", "2"}
    assert real_nos.isdisjoint(nonstop_nos)


def test_get_h1_real_target_nos_ignores_h2_mode_column(dr_excel_path):
    """NO 4는 H2 시트에 '무중단'으로 재기재됐지만, H1 기준 실전환이므로 여전히 real_nos에 남는다."""
    real_nos = excel_loader.get_h1_real_target_nos(excel_path=str(dr_excel_path))
    assert "4" in real_nos


# ─────────────────────────────────────────────
# load_extra_h2_items()
# ─────────────────────────────────────────────

def test_load_extra_h2_items_returns_only_h1_real_targets(dr_excel_path, monkeypatch):
    """extra_details는 173대(H1 무중단) 목록에 없는, H1 실전환 대상만 담고 is_extra=True가 붙는다."""
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    extra = dr_data.load_extra_h2_items(ticket_map={}, use_jira=False)

    extra_nos = {d["no"] for d in extra}
    assert extra_nos == {"3", "4"}
    assert all(d["is_extra"] is True for d in extra)


def test_load_extra_h2_items_disjoint_from_173_scope(dr_excel_path, monkeypatch):
    """extra_details의 no 집합은 dr_data.load_items('H2')(173대 스코프)의 no 집합과 겹치지 않는다."""
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    h2_scoped = dr_data.load_items("H2")
    scoped_nos = {i["no"] for i in get_targets(h2_scoped)}

    extra = dr_data.load_extra_h2_items(ticket_map={}, use_jira=False)
    extra_nos = {d["no"] for d in extra}

    assert scoped_nos == {"1", "2"}
    assert extra_nos.isdisjoint(scoped_nos)


def test_load_extra_h2_items_use_jira_false_skips_matching(dr_excel_path, monkeypatch):
    """use_jira=False면 JIRA 재조회 없이 엑셀 완료표기(상반기완료=O)만으로 판정한다."""
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    def _boom(*a, **kw):
        raise AssertionError("use_jira=False인데 외부 JIRA 조회를 시도했다")

    monkeypatch.setattr(dr_data, "_collect_external", _boom)

    extra = dr_data.load_extra_h2_items(ticket_map={}, use_jira=False)
    assert {d["no"] for d in extra} == {"3", "4"}


def test_load_extra_h2_items_empty_when_no_real_targets(dr_excel_path, monkeypatch):
    """H1 실전환 대상이 하나도 없으면 빈 리스트를 반환한다 (하반기 엑셀 병합 결과와 무관)."""
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})
    monkeypatch.setattr(dr_data, "load_dr_items_merged", lambda half: load_dr_items_merged(half=half, excel_path=str(dr_excel_path)))
    monkeypatch.setattr(dr_data, "get_h1_real_target_nos", lambda: set())

    extra = dr_data.load_extra_h2_items(ticket_map={}, use_jira=False)
    assert extra == []


# ─────────────────────────────────────────────
# /dr 라우트 - extra_details가 500 없이 응답에 반영되는지 (가볍게)
# ─────────────────────────────────────────────

def test_dr_dashboard_h2_includes_extra_details_without_500(dr_excel_path, monkeypatch):
    """
    /dr?half=H2 요청이 extra_details(상반기 실전환 참고 목록)를 포함해도 500 에러 없이
    렌더링된다. tests/test_template_routes.py의 외부 호출 차단 전략을 그대로 따른다
    (JIRA/Polestar/CMDB는 몽키패치로 무해화하고, 실제 코드 경로는 그대로 태운다).
    """
    from fastapi.testclient import TestClient


    from app.core import jira_client
    from app.main import app


    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})
    monkeypatch.setattr(jira_client.jira, "get_dr_tickets", lambda: [])
    monkeypatch.setattr(jira_client.jira, "search", lambda jql, fields=None: [])


    client = TestClient(app)
    resp = client.get("/dr", params={"half": "H2"})


    assert resp.status_code == 200
    assert "unhashable" not in resp.text.lower()
    # 실전환 참고 대상(NO 3,4)의 시스템명이 화면에 나타나야 한다 (extra_details 려더링 확인)
    assert "실전환시스템1" in resp.text
    assert "실전환시스템2" in resp.text
    # 173대 스코프(무중단) 대상도 여전히 같이 나와야 한다
    assert "무중단시스템1" in resp.text
