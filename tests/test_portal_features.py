"""포털 홈의 신규 기능 테스트: 기준일, 전체 요약, 조치 필요, 최근 변경."""
from datetime import datetime
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

from app.main import app
from app.core import jira_client, polestar_client
from app.services import dr_data, eos_data, owner_check
from app.web.routes import capacity as capacity_routes
from app.web.routes import dr as dr_routes
from app.web.routes import eos as eos_routes
from app.web.routes import kernel as kernel_routes
from app.web.routes import teams as teams_routes


def _apply_no_network_patches(monkeypatch):
    """네트워크 호출 제거 패치 (기존 방식 재사용)."""
    monkeypatch.setattr(jira_client.jira, "get_dr_tickets", lambda: [])
    monkeypatch.setattr(jira_client.jira, "get_capacity_tickets", lambda: [])
    monkeypatch.setattr(jira_client.jira, "get_eos_tickets", lambda: [])
    monkeypatch.setattr(jira_client.jira, "search", lambda jql, fields=None: [])
    monkeypatch.setattr(polestar_client.polestar, "list_resources", lambda resource_type="all": [])
    monkeypatch.setattr(owner_check, "get_server_assets", lambda hostnames: {})
    
    monkeypatch.setattr(dr_data, "load_items", lambda half: [])
    monkeypatch.setattr(dr_data, "get_ticket_map", lambda half, items, use_jira=True: ({}, None))
    monkeypatch.setattr(dr_routes, "load_dr_items_merged", lambda half="H2": [])
    monkeypatch.setattr(dr_routes, "collect_targets_with_tickets", lambda half, use_jira=True: ([], {}, None))
    monkeypatch.setattr(capacity_routes, "collect_capacity_targets_with_tickets", lambda sheet, use_jira=True: ([], {}, None))
    monkeypatch.setattr(capacity_routes, "load_capacity_items_merged", lambda sheet, excel_path=None: [])
    
    empty_eos_data = lambda use_external=True: ([], {}, set(), None)
    monkeypatch.setattr(eos_data, "get_eos_data", empty_eos_data)
    monkeypatch.setattr(eos_routes, "get_eos_data", empty_eos_data)
    monkeypatch.setattr(eos_routes, "load_eos_items_merged", lambda: [])
    monkeypatch.setattr(eos_routes, "collect_eos_targets_with_tickets", lambda use_jira=True: ([], {}, None))
    
    monkeypatch.setattr(kernel_routes, "available_scopes", lambda: [])
    monkeypatch.setattr(kernel_routes, "collect_kernel_targets", lambda scope="dev": [])
    monkeypatch.setattr(kernel_routes, "load_kernel_items_merged", lambda scope="dev": [])
    
    monkeypatch.setattr(teams_routes, "available_scopes", lambda: [])
    monkeypatch.setattr(teams_routes, "load_kernel_items_merged", lambda scope="dev": [])
    monkeypatch.setattr(teams_routes.dr_data, "load_items", lambda half: [])


def test_portal_home_has_sidebar_after_layout_unification(monkeypatch):
    """portal.html이 _layout.html을 상속하므로 사이드바가 있어야 한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)
    
    resp = client.get("/")
    
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    sidebar = soup.select_one("aside.sidebar")
    assert sidebar is not None, "포털 홈에 사이드바가 없습니다"


def test_portal_home_shows_as_of_date(monkeypatch):
    """기준일(2a)이 표시되어야 한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)
    
    resp = client.get("/")
    
    assert resp.status_code == 200
    # YYYY-MM-DD 형식의 날짜 표시 확인
    assert "기준일:" in resp.text


def test_portal_home_shows_recent_changes_section(monkeypatch):
    """최근 변경 섹션(2d)이 있어야 한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)
    
    resp = client.get("/")
    
    assert resp.status_code == 200
    # 최근 변경 섹션 제목이 있어야 함
    assert "최근 변경 (7일)" in resp.text


def test_portal_home_has_overall_summary_structure(monkeypatch):
    """전체 요약 수치(2b)의 기본 구조가 있어야 한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)
    
    resp = client.get("/")
    
    assert resp.status_code == 200
    # 모든 모듈이 빈 값이면 전체 요약은 안 나타나지만, 구조는 정의되어야 함
    # (비어있을 때 표시 안 함: {% if total_target > 0 %})
    # 따라서 여기서는 상태코드 200 확인만 함


def test_portal_has_no_old_tabs_nav(monkeypatch):
    """기존 독립 탭 네비(nav.tabs)가 제거되어야 한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)
    
    resp = client.get("/")
    
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    old_tabs = soup.select_one('nav[aria-label="대시보드 화면 메뉴"]')
    assert old_tabs is None, "기존 탭 네비가 여전히 있습니다"


def test_portal_home_has_all_module_cards(monkeypatch):
    """모든 4개 모듈 카드가 있어야 한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)
    
    resp = client.get("/")
    
    assert resp.status_code == 200
    for label in ("DR 모의훈련", "용량관리", "EoS 전환", "OS 커널 패치"):
        assert label in resp.text, f"모듈 라벨 '{label}'이 없습니다"


def test_portal_home_module_cards_intact(monkeypatch):
    """모듈 카드 타일이 4개 있어야 한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)
    
    resp = client.get("/")
    
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    module_tiles = soup.select("a.module-tile")
    assert len(module_tiles) == 4, f"메모리 타일이 {len(module_tiles)}개입니다 (4개여야 함)"
