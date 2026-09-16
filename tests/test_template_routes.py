# tests/test_template_routes.py
"""
회귀 테스트 - TemplateResponse() 호출 시그니처 버그(starlette 1.x 업그레이드로
"unhashable type: 'dict'" 500 에러가 나던 그 버그) 재발 방지.

기존 tests/test_smoke.py는 /health, /metrics만 확인해서 실제로 템플릿을 렌더링하는
14개 라우트(portal/dashboard/capacity/eos/kernel 등)의 회귀를 전혀 잡지 못했다 -
바로 이 사각지대 때문에 프로덕션에서 전체 웹 UI가 내려가는 사고가 났다.

여기서는 각 라우트가 실제로 templates.TemplateResponse(request, name, context)를
올바르게(포지셔널 인자 순서 정상) 호출하는지를, 그 라우트가 의존하는 외부
호출(JIRA/Polestar/Insight CMDB)만 몽키패치로 걷어내고 실제 HTTP GET으로 확인한다.

외부 호출을 몽키패치하는 이유:
- `TestClient(app)`을 `with` 없이 쓰면 lifespan(startup)의 prewarm_*()들은 안 돌지만,
  각 라우트 핸들러 자체가 요청 안에서 직접 JIRA/Polestar/CMDB를 부른다(캐시가 비어
  있으면 요청 스레드가 응답을 기다린다). 실제 jira.sinc.co.kr으로 나가면(dummy 호스트가
  아니라 conftest가 못 심는 실제 .env 값) 수십 초~분 단위로 걸리고, 그 자체가 이번
  회귀 테스트가 확인하려는 "템플릿 호출 시그니처"와 무관한 실패 지점을 늘린다.
- 대신 jira_client/polestar_client 싱글턴의 조회 메서드, 그리고
  app.services.owner_check가 바인딩해 쓰는 get_server_assets()를 몽키패치해
  "외부 조회 성공(빈 결과)"로 응답하게 만든다. 이러면 각 라우트는 실제 코드 경로
  (JIRA 매칭 -> 완료율 계산 -> 템플릿 렌더링)를 전부 타면서도 네트워크에 나가지
  않는다 - 이번 버그가 있던 지점(TemplateResponse 호출)까지 실제로 도달시키는 것이
  핵심이라 데이터 자체를 가짜로 만들기보다 외부 호출만 무해하게 막는 쪽을 택했다.

엑셀/DB는 실제 리포지토리의 data/*.xlsx, data/tracker.db를 그대로 쓴다 - 이미
존재하는 파일이라 별도 픽스처가 필요 없고, 로컬 파일 읽기라 느리지 않다.
"""
from fastapi.testclient import TestClient

from app.core import jira_client, polestar_client
from app.services import capacity_data, dr_data, eos_data, owner_check
from app.web.routes import dr as dr_routes
from app.web.routes import eos as eos_routes
from app.web.routes import kernel as kernel_routes
from app.main import app

# 500 에러 응답 본문에서 이번 버그 계열(포지셔널 인자 밀림)을 잡기 위한 문자열.
# starlette가 raise한 예외가 그대로 노출되지 않는 프로덕션 설정이어도, 최소한
# status_code만은 500이 아니어야 한다는 것이 핵심 단언이다.
_REGRESSION_MARKERS = ("unhashable", "typeerror")

# (HTTP 메서드, 경로, 템플릿 파일명) - PRODUCTION INCIDENT 스코프의 14개 콜사이트와 1:1 대응
ROUTES = [
    ("GET", "/", "portal.html"),
    ("GET", "/dr", "dashboard.html"),
    ("GET", "/logs", "logs.html"),
    ("GET", "/remind-preview", "remind_preview.html"),
    ("GET", "/owner-check", "owner_check.html"),
    ("GET", "/capacity", "capacity.html"),
    ("GET", "/capacity/remind-preview", "capacity_remind_preview.html"),
    ("GET", "/capacity/owner-check", "capacity_owner_check.html"),
    ("GET", "/eos", "eos.html"),
    ("GET", "/eos/remind-preview", "eos_remind_preview.html"),
    ("GET", "/eos/owner-check", "eos_owner_check.html"),
    ("GET", "/eos/plan-chat", "eos_plan_chat.html"),
    ("GET", "/kernel", "kernel.html"),
    ("GET", "/kernel/owner-check", "kernel_owner_check.html"),
]


def _fake_search(jql, fields=None):
    """jira.search()도 evidence_check(증적 티켓 상태 조회)가 부르므로 같이 막아준다."""
    return []


def _apply_no_network_patches(monkeypatch):
    """
    JIRA/Polestar/CMDB 조회만 무해한 빈 결과로 바꿔치기한다.

    싱글턴 객체(jira, polestar)의 메서드를 직접 패치하므로, 어느 모듈이
    `from app.core.jira_client import jira`로 참조를 들고 있든 같은 객체라 전부
    적용된다. 반면 get_server_assets()는 함수라 owner_check.py가
    `from app.core.insight_client import get_server_assets`로 이름을 이미
    바인딩해뒀으므로, app.core.insight_client 쪽이 아니라 owner_check 모듈에
    바인딩된 이름 쪽을 패치해야 실제로 먹는다.
    """
    monkeypatch.setattr(jira_client.jira, "get_dr_tickets", lambda: [])
    monkeypatch.setattr(jira_client.jira, "get_capacity_tickets", lambda: [])
    monkeypatch.setattr(jira_client.jira, "get_eos_tickets", lambda: [])
    monkeypatch.setattr(jira_client.jira, "search", _fake_search)
    monkeypatch.setattr(polestar_client.polestar, "list_resources", lambda resource_type="all": [])
    monkeypatch.setattr(owner_check, "get_server_assets", lambda hostnames: {})

    # 저장소에 포함되지 않는 운영 엑셀(data/*.xlsx)에 기대지 않고도 템플릿 경로를
    # 끝까지 실행할 수 있도록, 각 도메인의 데이터 경계를 빈 테스트 데이터로 바꾼다.
    monkeypatch.setattr(dr_data, "load_items", lambda half: [])
    monkeypatch.setattr(
        dr_data,
        "get_ticket_map",
        lambda half, items, use_jira=True: ({}, None),
    )
    monkeypatch.setattr(dr_routes, "load_dr_items_merged", lambda half="H2": [])
    monkeypatch.setattr(
        capacity_data,
        "get_matched_items",
        lambda sheet, use_jira=True: ([], {}, None),
    )

    empty_eos_data = lambda use_external=True: ([], {}, set(), None)
    monkeypatch.setattr(eos_data, "get_eos_data", empty_eos_data)
    monkeypatch.setattr(eos_routes, "get_eos_data", empty_eos_data)
    monkeypatch.setattr(eos_routes, "load_eos_items_merged", lambda: [])

    monkeypatch.setattr(kernel_routes, "available_scopes", lambda: [])
    monkeypatch.setattr(
        kernel_routes,
        "load_kernel_items_merged",
        lambda scope="dev": [],
    )


def test_all_template_routes_render_without_500(monkeypatch):
    """
    이번 버그가 있었으면 14개 라우트 전부가 500(unhashable type: 'dict')이었다.
    수정 후에는 전부 500이 아니어야 하고, 이번 버그 계열 문구도 응답 본문에 없어야 한다.
    """
    _apply_no_network_patches(monkeypatch)

    # `with TestClient(app) as client:` 를 쓰면 lifespan(startup)의 prewarm_*()가
    # 실제 백그라운드 스레드로 JIRA 조회를 또 시작한다 (test_smoke.py와 동일 이유로 회피).
    client = TestClient(app)

    failures = []
    for method, path, template_name in ROUTES:
        resp = client.request(method, path)
        body_lower = resp.text.lower()

        if resp.status_code == 500:
            failures.append(f"{path} ({template_name}): status=500 body[:200]={resp.text[:200]!r}")
            continue

        for marker in _REGRESSION_MARKERS:
            if marker in body_lower:
                failures.append(
                    f"{path} ({template_name}): status={resp.status_code} "
                    f"but response body contains regression marker {marker!r}"
                )

    assert not failures, "TemplateResponse 회귀 감지:\n" + "\n".join(failures)


def test_portal_home_returns_200_with_all_module_cards(monkeypatch):
    """
    포털 홈("/")은 가장 단순한 라우트 - DR/용량관리/EoS/커널패치 네 모듈의 완료율
    카드를 집계해 보여줄 뿐이다. 여기선 상태코드까지 정확히 200인지, 그리고 실제로
    portal.html이 렌더링해 4개 모듈 라벨이 응답 본문에 담기는지까지 확인한다
    (템플릿 렌더링 자체가 통과했다는 더 강한 증거).
    """
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)

    resp = client.get("/")

    assert resp.status_code == 200
    for label in ("DR 모의훈련", "용량관리", "EoS 전환", "OS 커널 패치"):
        assert label in resp.text


def test_dr_dashboard_returns_200_and_renders_details_table(monkeypatch):
    """DR 대시보드 - 프로덕션 장애 리포트에 명시된 첫 번째 재현 경로(home.py:122)와
    같은 계열인 dr.py:187(dashboard.html)을 별도로 한 번 더 강하게 검증한다."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)

    resp = client.get("/dr")

    assert resp.status_code == 200
    assert "unhashable" not in resp.text.lower()


def test_logs_page_returns_200(monkeypatch):
    """변경 이력 화면 - 외부 호출이 전혀 없는(DB 조회만) 가장 가벼운 템플릿 라우트."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)

    resp = client.get("/logs")

    assert resp.status_code == 200


def test_kernel_dashboard_returns_200(monkeypatch):
    """커널패치 대시보드 - JIRA 티켓맵이 없는(빈 dict) 특수 케이스라 별도로 확인."""
    _apply_no_network_patches(monkeypatch)
    client = TestClient(app)

    resp = client.get("/kernel")

    assert resp.status_code == 200
