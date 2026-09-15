# tests/conftest.py
"""
pytest 전역 설정.

app.config.settings 는 모듈 최상단에서 Settings()를 즉시 생성하고, 기본값이 없는
필수 필드가 비어 있으면 pydantic.ValidationError로 임포트 자체가 실패한다(app/config.py
맨 아래 `settings = Settings()`). CI에는 실제 .env 파일이 없으므로, 어떤 테스트
모듈이든 `import app.xxx`를 하기 전에 이 conftest.py가 먼저 로드되어 더미 환경변수를
심어둬야 한다.

pytest는 tests/ 디렉터리를 수집할 때 이 conftest.py를 그 안의 테스트 모듈보다 먼저
임포트하므로, 여기서 os.environ에 값을 채워두는 것만으로 아래의 모든 test_*.py에
안전하게 적용된다 (pydantic-settings는 실제 os 환경변수를 .env 파일 값보다 우선한다 -
.env가 없어도 이 방식이 동작하는 이유).

실제 비밀값(.env)은 여기서 절대 읽거나 참조하지 않는다 - 전부 테스트 전용 더미 값이다.
"""
import os

# app/config.py Settings에서 기본값이 없어 반드시 채워야 하는 필드 목록
# (jira_url, jira_pat, jira_project, sender_team, sender_name, capacity_sender_name,
#  dashboard_url, admin_users, capacity_admin_users, eos_admin_users)
_DUMMY_ENV = {
    "JIRA_URL": "https://jira.dummy.test",
    "JIRA_PAT": "dummy-jira-pat",
    "JIRA_PROJECT": "DUMMY",
    "SENDER_TEAM": "테스트팀",
    "SENDER_NAME": "테스트담당자",
    "CAPACITY_SENDER_NAME": "테스트용량담당자",
    "DASHBOARD_URL": "http://dummy-dashboard.test:8000",
    "ADMIN_USERS": "테스트관리자",
    "CAPACITY_ADMIN_USERS": "테스트용량관리자",
    "EOS_ADMIN_USERS": "테스트EoS관리자",
}

for _key, _value in _DUMMY_ENV.items():
    os.environ.setdefault(_key, _value)


import pytest  # noqa: E402  (환경변수 주입이 끝난 뒤에 임포트되어야 순서가 보장된다)


@pytest.fixture(scope="session", autouse=True)
def _dummy_settings_env():
    """세션 전체에서 더미 환경변수가 유지되는지 확인하는 안전장치.

    위 모듈 최상단 주입만으로 충분하지만(conftest는 테스트 모듈보다 먼저 로드됨),
    다른 conftest/플러그인이 os.environ을 건드릴 가능성에 대비해 한 번 더 검증한다.
    """
    missing = [k for k in _DUMMY_ENV if k not in os.environ]
    assert not missing, f"더미 환경변수 누락: {missing}"
    yield
