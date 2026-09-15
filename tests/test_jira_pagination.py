# tests/test_jira_pagination.py
"""
JiraClient.search()의 페이징/병렬조회/중복제거 로직 테스트.

실제 JIRA로 네트워크 호출을 하지 않기 위해 `JiraClient._search_page`(실제 HTTP GET을
하는 메서드)만 patch.object로 가짜 함수로 바꿔치기하고, search()의 나머지 로직
(첫 페이지로 total 파악 -> 남은 offset 병렬조회 -> key 기준 병합)은 실제 코드를
그대로 실행시켜 검증한다.

`app.core.jira_client`는 모듈 최하단에서 `jira = JiraClient()`를 즉시 생성하는데,
JiraClient.__init__은 settings.jira_url/jira_pat을 읽으므로 conftest.py가 심어둔
더미 JIRA_URL/JIRA_PAT 환경변수가 먼저 로드되어 있어야 임포트가 실패하지 않는다
(conftest는 pytest가 자동으로 먼저 로드하므로 여기서 따로 신경 쓸 필요는 없다).

테스트마다 모듈 최상단 싱글턴 `jira`를 공유해서 쓰지 않고 `JiraClient()`를 직접
새로 만든다 - 테스트 간 상태(세션 등)를 격리하기 위함이며, __init__은 requests.Session
객체와 재시도 어댑터만 만들 뿐 네트워크 호출을 하지 않으므로 안전하다.
"""
from unittest.mock import patch

from app.core.jira_client import JiraClient


def _issue(key: str, summary: str = "") -> dict:
    """테스트용 가짜 JIRA 이슈 객체 생성."""
    return {"key": key, "fields": {"summary": summary}}


def test_single_page_covers_all():
    """첫 페이지만으로 total을 다 채우면(len(issues) >= total) 추가 페이지를 조회하지
    않아야 한다 - _search_page가 정확히 1회만 호출됐는지로 검증한다."""
    client = JiraClient()
    fake_issues = [_issue(f"TICKET-{i}") for i in range(50)]

    def fake_search_page(url, jql, fields_param, start_at, page_size):
        assert start_at == 0
        return {"issues": fake_issues, "total": 50}

    with patch.object(JiraClient, "_search_page", side_effect=fake_search_page) as mock_page:
        result = client.search("project = DUMMY")

    assert result == fake_issues
    assert mock_page.call_count == 1


def test_exact_multiple_pagination():
    """total=300, 페이지당 100건씩 딱 3페이지로 나뉘는 경우 - 3번의 호출(0, 100, 200)로
    300건 전부가 중복 없이 모여야 한다."""
    client = JiraClient()
    all_issues = {
        0: [_issue(f"TICKET-{i}") for i in range(1, 101)],
        100: [_issue(f"TICKET-{i}") for i in range(101, 201)],
        200: [_issue(f"TICKET-{i}") for i in range(201, 301)],
    }

    def fake_search_page(url, jql, fields_param, start_at, page_size):
        return {"issues": all_issues[start_at], "total": 300}

    with patch.object(JiraClient, "_search_page", side_effect=fake_search_page) as mock_page:
        result = client.search("project = DUMMY")

    assert mock_page.call_count == 3
    called_starts = sorted(c.args[3] for c in mock_page.call_args_list)
    assert called_starts == [0, 100, 200]

    keys = {issue["key"] for issue in result}
    assert len(result) == 300
    assert keys == {f"TICKET-{i}" for i in range(1, 301)}


def test_uneven_last_page():
    """total=250, 첫 페이지 100건 -> 남은 offset은 [100, 200] 이어야 하고(300이 아님),
    마지막 부분 페이지(offset=200, 50건)까지 포함해 총 250건이 모여야 한다."""
    client = JiraClient()
    all_issues = {
        0: [_issue(f"TICKET-{i}") for i in range(1, 101)],
        100: [_issue(f"TICKET-{i}") for i in range(101, 201)],
        200: [_issue(f"TICKET-{i}") for i in range(201, 251)],  # 50건짜리 마지막 페이지
    }

    def fake_search_page(url, jql, fields_param, start_at, page_size):
        return {"issues": all_issues[start_at], "total": 250}

    with patch.object(JiraClient, "_search_page", side_effect=fake_search_page) as mock_page:
        result = client.search("project = DUMMY")

    called_starts = sorted(c.args[3] for c in mock_page.call_args_list)
    assert called_starts == [0, 100, 200]  # 300은 절대 요청되면 안 된다(범위 밖 offset)

    keys = {issue["key"] for issue in result}
    assert len(result) == 250
    assert keys == {f"TICKET-{i}" for i in range(1, 251)}


def test_empty_result():
    """첫 페이지가 issues=[], total=0 이면 (issues가 비어 step=0이 되어
    range(step, total, step) -> range(0, 0, 0)로 ValueError가 날 수 있는 지점인데)
    `if not issues or len(issues) >= total: return issues` 가드가 먼저 걸려
    추가 페이지 조회 없이 즉시 빈 리스트를 반환해야 한다."""
    client = JiraClient()

    def fake_search_page(url, jql, fields_param, start_at, page_size):
        assert start_at == 0
        return {"issues": [], "total": 0}

    with patch.object(JiraClient, "_search_page", side_effect=fake_search_page) as mock_page:
        result = client.search("project = DUMMY")

    assert result == []
    assert mock_page.call_count == 1


def test_duplicate_key_across_pages_deduplicated():
    """페이징 도중 티켓 생성/삭제로 offset이 밀려 같은 key가 첫 페이지와 이후 offset
    페이지에 겹쳐 나타나는 경우(코드 docstring에 명시된 케이스) - by_key.setdefault()
    로직에 따라 먼저 본 첫 페이지 버전이 유지되고, 이후 페이지의 동일 key는 버려져야
    한다. 두 버전의 summary를 다르게 줘서 어느 쪽이 살아남는지 확인한다."""
    client = JiraClient()
    first_page_issues = [_issue("TICKET-5", summary="첫페이지-원본")] + [
        _issue(f"TICKET-{i}") for i in range(6, 105)
    ]  # 총 100건 (TICKET-5 ~ TICKET-104)

    def fake_search_page(url, jql, fields_param, start_at, page_size):
        if start_at == 0:
            return {"issues": first_page_issues, "total": 150}
        if start_at == 100:
            # offset 페이지에 TICKET-5가 밀려 들어온 상황(중복) - summary가 다르다
            duplicated = _issue("TICKET-5", summary="offset페이지-중복본")
            rest = [_issue(f"TICKET-{i}") for i in range(105, 155)]
            return {"issues": [duplicated, *rest], "total": 150}
        raise AssertionError(f"unexpected start_at={start_at}")

    with patch.object(JiraClient, "_search_page", side_effect=fake_search_page):
        result = client.search("project = DUMMY")

    matches = [issue for issue in result if issue["key"] == "TICKET-5"]
    assert len(matches) == 1
    assert matches[0]["fields"]["summary"] == "첫페이지-원본"


def test_parallel_execution_isolation():
    """ThreadPoolExecutor로 여러 offset 페이지를 병렬 조회할 때, 그 중 일부 워커의
    결과가 조용히 누락되지 않고 전부(5페이지 x 100건 = 500건) 최종 결과에 모여야 한다."""
    client = JiraClient()
    all_issues = {
        start: [_issue(f"TICKET-{i}") for i in range(start + 1, start + 101)]
        for start in (0, 100, 200, 300, 400)
    }

    def fake_search_page(url, jql, fields_param, start_at, page_size):
        return {"issues": all_issues[start_at], "total": 500}

    with patch.object(JiraClient, "_search_page", side_effect=fake_search_page) as mock_page:
        result = client.search("project = DUMMY")

    assert mock_page.call_count == 5
    keys = {issue["key"] for issue in result}
    assert len(result) == 500
    assert keys == {f"TICKET-{i}" for i in range(1, 501)}
