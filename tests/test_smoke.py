# tests/test_smoke.py
"""
스모크 테스트 - 앱이 뜨는지, 설정이 로드되는지, 대표 엔드포인트가 응답하는지만 확인한다.

app.main의 lifespan(startup)은 init_db/start_scheduler 외에 prewarm_eos/prewarm_dr/
prewarm_capacity가 실제 JIRA·Polestar·Confluence에 네트워크 호출을 하므로 CI에서는
절대 실행하면 안 된다. `TestClient(app)`을 `with` 블록 없이 사용하면 starlette가
lifespan(startup/shutdown) 이벤트를 아예 보내지 않으므로 그 로직이 돌지 않는다 -
여기서 검증하는 엔드포인트(/health, /metrics)는 DB나 스케줄러, 외부 캐시에 의존하지
않으므로 이 방식으로 충분하다.
"""
from fastapi.testclient import TestClient


def test_app_importable():
    """app.main을 임포트하는 것만으로 예외가 나지 않아야 한다 (설정/라우터 로딩 확인)."""
    import app.main  # noqa: F401


def test_config_loads():
    """app.config.settings가 conftest에서 심은 더미 값으로 정상 로드됐는지 확인한다."""
    from app.config import Settings, settings

    assert isinstance(settings, Settings)
    assert settings.jira_url == "https://jira.dummy.test"
    assert settings.jira_project == "DUMMY"
    assert settings.sender_team == "테스트팀"
    assert settings.admin_users == "테스트관리자"
    assert settings.dashboard_url.startswith("http://")


def test_health_endpoint():
    """가장 가벼운 헬스체크 라우트(app/web/routes/misc.py) - DB/외부 연동 없이 고정 응답만 돌려준다."""
    from app.main import app

    # `with TestClient(app) as client:` 를 쓰면 lifespan(startup)이 실행되어
    # 실제 JIRA/Polestar 호출이 발생하므로, 일부러 컨텍스트 매니저 없이 사용한다.
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_endpoint():
    """Instrumentator().expose()가 붙인 /metrics - DB/외부 연동 없이 동작해야 한다."""
    from app.main import app

    client = TestClient(app)
    response = client.get("/metrics")

    assert response.status_code == 200
