# tests/test_jira_truncated_response.py
"""
응답 본문이 중간에 끊겼을 때(_search_page) 그 페이지만 다시 받아오는지.

HTTPAdapter에 붙인 Retry는 연결 수립 실패와 5xx를 다루지만, 연결은 맺힌 뒤 본문을
받다가 끊기는 경우는 안 잡는다. 그래서 티켓 수백 건을 받는 조회가
"JIRA 연동 실패: Connection broken: IncompleteRead(...)" 하나로 통째로 실패했다.
"""
from unittest.mock import patch

import pytest
import requests

from app.core.jira_client import PAGE_RETRY, JiraClient


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _broken():
    return requests.exceptions.ChunkedEncodingError(
        "('Connection broken: IncompleteRead(8090 bytes read, 102 more expected)',)"
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """재시도 간격만큼 테스트가 느려지지 않게"""
    monkeypatch.setattr("app.core.jira_client.time.sleep", lambda _s: None)


def test_본문이_끊기면_다시_받아온다():
    client = JiraClient()
    payload = {"issues": [{"key": "A-1", "fields": {}}], "total": 1}
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params["startAt"])
        if len(calls) == 1:
            raise _broken()
        return _Resp(payload)

    with patch.object(client.session, "get", side_effect=fake_get):
        assert client._search_page("u", "jql", None, 0, 100) == payload
    assert len(calls) == 2   # 처음 실패 + 재시도 성공


def test_계속_끊기면_마지막에_예외를_올린다():
    client = JiraClient()

    def fake_get(url, params=None, timeout=None):
        raise _broken()

    with patch.object(client.session, "get", side_effect=fake_get) as mock_get:
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            client._search_page("u", "jql", None, 0, 100)
    # 무한 재시도가 아니라 정해진 횟수까지만
    assert mock_get.call_count == PAGE_RETRY + 1


def test_잘린_본문이라_json이_깨져도_다시_받아온다():
    client = JiraClient()
    payload = {"issues": [], "total": 0}
    calls = []

    class _BadJson(_Resp):
        def json(self):
            raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)

    def fake_get(url, params=None, timeout=None):
        calls.append(1)
        return _BadJson(None) if len(calls) == 1 else _Resp(payload)

    with patch.object(client.session, "get", side_effect=fake_get):
        assert client._search_page("u", "jql", None, 0, 100) == payload
    assert len(calls) == 2


def test_그냥_실패는_재시도하지_않는다():
    """404 같은 건 다시 받아도 결과가 같다 - 괜히 시간만 끈다"""
    client = JiraClient()

    class _Err(_Resp):
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("404 Not Found")

    with patch.object(client.session, "get", return_value=_Err(None)) as mock_get:
        with pytest.raises(requests.exceptions.HTTPError):
            client._search_page("u", "jql", None, 0, 100)
    assert mock_get.call_count == 1
