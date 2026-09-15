# tests/test_deps.py
"""
app.web.deps 공통 검증 로직 테스트 (require_updated_by, resolve_owner).

app.web.deps는 모듈 최상단에서 Jinja2Templates(directory=...)를 생성하는데,
이는 app/web/templates 디렉터리가 실제로 존재해야 임포트가 성공한다 - 이미
tests/test_smoke.py가 app.main을 통해 이 모듈을 간접적으로 임포트하며 검증된
바 있으므로 여기서 직접 임포트해도 안전하다. app.config.settings는 건드리지
않지만, conftest.py가 더미 환경변수를 먼저 심어두므로 다른 테스트와 충돌 없이
독립적으로 실행 가능하다.
"""
import pytest
from fastapi import HTTPException

from app.web.deps import require_updated_by, resolve_owner


def test_require_updated_by_valid_name():
    """정상적인 이름(2글자 이상)은 그대로 반환된다."""
    assert require_updated_by("홍길동") == "홍길동"


def test_require_updated_by_strips_whitespace():
    """앞뒤 공백은 제거된 뒤, 제거된 값 기준으로 길이를 검사해 반환한다."""
    assert require_updated_by("  김철수  ") == "김철수"


def test_require_updated_by_too_short_raises_400():
    """1글자 이름은 400 에러와 함께 안내 메시지를 담아 예외를 발생시킨다."""
    with pytest.raises(HTTPException) as exc_info:
        require_updated_by("김")

    assert exc_info.value.status_code == 400
    assert "2글자 이상" in exc_info.value.detail


def test_require_updated_by_empty_string_raises_400():
    """빈 문자열도 400 에러를 발생시킨다."""
    with pytest.raises(HTTPException) as exc_info:
        require_updated_by("")

    assert exc_info.value.status_code == 400


def test_require_updated_by_none_raises_400():
    """None이 들어와도 TypeError 없이 `(updated_by or "").strip()`으로 안전하게 처리되어 400을 낸다."""
    with pytest.raises(HTTPException) as exc_info:
        require_updated_by(None)

    assert exc_info.value.status_code == 400


def test_require_updated_by_whitespace_only_raises_400():
    """공백만 있는 문자열은 strip 후 빈 문자열이 되어 400 에러를 낸다."""
    with pytest.raises(HTTPException) as exc_info:
        require_updated_by("   ")

    assert exc_info.value.status_code == 400


def test_resolve_owner_admin_can_set():
    """관리자가 담당자를 요청하면 요청한 값 그대로 반환한다."""
    result = resolve_owner("새담당자", "admin1", {"admin1", "admin2"})
    assert result == "새담당자"


def test_resolve_owner_non_admin_returns_none():
    """관리자가 아닌 사용자가 요청하면 예외 없이 조용히 None을 반환한다 (기존 담당자 유지)."""
    result = resolve_owner("새담당자", "일반사용자", {"admin1", "admin2"})
    assert result is None


def test_resolve_owner_empty_requested_returns_none():
    """요청 관리자여도 요청값이 빈 문자열이면 None을 반환한다 (관리자 여부와 무관하게 우선 차단)."""
    result = resolve_owner("", "admin1", {"admin1"})
    assert result is None


def test_resolve_owner_whitespace_only_requested_returns_none():
    """요청값이 공백만 있으면 strip 후 빈 문자열이 되어 None을 반환한다."""
    result = resolve_owner("   ", "admin1", {"admin1"})
    assert result is None


def test_resolve_owner_strips_whitespace_from_valid_value():
    """관리자가 보낸 유효한 값은 앞뒤 공백이 제거된 채로 반환된다."""
    result = resolve_owner("  새담당자  ", "admin1", {"admin1"})
    assert result == "새담당자"
