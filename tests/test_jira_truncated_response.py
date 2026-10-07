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


class _HttpError(requests.exceptions.HTTPError):
    """status_code를 들고 있는 HTTPError (raise_for_status가 만드는 것과 같은 모양)"""
    def __init__(self, status):
        super().__init__(f"{status} error")
        self.response = type("R", (), {"status_code": status})()


class TestGetIssuesByKeys:
    """
    묶음 조회가 실패했을 때 한 건씩 다시 쏠지 말지.

    타임아웃에도 한 건씩 돌리면 이미 느려진 서버에 같은 요청을 묶음 크기만큼 더
    보내게 된다 (40키 x 30초). 실제로 그 상태의 로그가 관측됐다.
    """

    def test_없는_키라_400이면_한_건씩_다시_조회한다(self):
        client = JiraClient()
        calls = []

        def fake_search(jql, fields=None):
            calls.append(jql)
            if jql.startswith("key in"):
                raise _HttpError(400)
            return [{"key": "A-1", "fields": {}}]

        with patch.object(client, "search", side_effect=fake_search):
            got = client.get_issues_by_keys(["A-1", "A-2"], fields=["summary"])
        assert len(got) == 2
        assert calls[0].startswith("key in")
        assert calls[1:] == ["key = A-1", "key = A-2"]

    def test_타임아웃이면_한_건씩_돌리지_않는다(self):
        client = JiraClient()
        calls = []

        def fake_search(jql, fields=None):
            calls.append(jql)
            raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)")

        with patch.object(client, "search", side_effect=fake_search):
            with pytest.raises(requests.exceptions.ReadTimeout):
                client.get_issues_by_keys(["A-1", "A-2", "A-3"], fields=["summary"])
        # 묶음 한 번만 시도하고 끝 - 키 수만큼 더 쏘지 않는다
        assert len(calls) == 1

    def test_500도_한_건씩_돌리지_않는다(self):
        client = JiraClient()
        calls = []

        def fake_search(jql, fields=None):
            calls.append(jql)
            raise _HttpError(500)

        with patch.object(client, "search", side_effect=fake_search):
            with pytest.raises(requests.exceptions.HTTPError):
                client.get_issues_by_keys(["A-1", "A-2"], fields=["summary"])
        assert len(calls) == 1
