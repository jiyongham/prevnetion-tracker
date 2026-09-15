# tests/test_matcher.py
"""
app.services.matcher 매칭 로직 테스트.

이 모듈은 app.config.settings 등 앱 전역 설정에 의존하지 않는 순수 함수 모음이라
conftest.py의 더미 환경변수 주입과 무관하게 안전하게 테스트할 수 있다.

특히 "매칭 오류"에 취약한 지점들 - IP 정규식 경계, 호스트명 최소 길이,
대소문자 구분, IP/호스트명 매칭의 우선순위 및 보강 관계, 중복 티켓 제거,
CMDB Key 직접 매칭, 여러 매칭 소스 병합 시 중복 제거 - 을 집중적으로 검증한다.
"""
from app.services.matcher import (
    IP_PATTERN,
    extract_ips,
    parse_excel_ips,
    match_items_by_ip,
    match_items_by_cmdb_key,
    merge_ticket_maps,
    MIN_HOSTNAME_LEN,
)


def test_extract_ips_basic():
    """가장 기본적인 IP 추출 - 문장 속 IP 하나를 정확히 뽑아낸다."""
    result = extract_ips("서버 IP: 10.20.30.40 연결됨")
    assert result == {"10.20.30.40"}


def test_ip_boundary_no_partial_match():
    """
    IP_PATTERN의 \\b(word boundary)는 숫자-숫자 사이를 경계로 보지 않는다
    (둘 다 \\w 문자라 경계가 성립하지 않음). 그래서 IP 뒤에 구분자 없이
    숫자가 바로 붙으면(엑셀 오타 등으로 "10.20.30.19900"처럼 되는 경우)
    마지막 옥텟 뒤의 \\b 조건이 깨져 전체 매칭 시도 자체가 실패한다.

    실제 동작 확인(사전에 인터랙티브로 검증):
        >>> import re
        >>> p = re.compile(r"\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b")
        >>> p.findall("10.20.30.19900")
        []
    즉 부분일치로 "10.20.30.199"만 뽑히는 게 아니라 아예 빈 리스트가 반환된다 -
    이는 "짧은 오매칭"이 아니라 "매칭 누락" 쪽으로 안전하게 fail-safe하다는 뜻.
    """
    assert IP_PATTERN.findall("10.20.30.19900") == []
    assert extract_ips("장비 10.20.30.19900 점검") == set()

    # 반대로 IP가 괄호/공백 등 비숫자 문자로 명확히 구분되면 정상적으로 각각 추출된다.
    result = extract_ips("서버1(10.20.30.40 )과 서버2(50.60.70.80)")
    assert result == {"10.20.30.40", "50.60.70.80"}


def test_parse_excel_ips_multiple_formats():
    """엑셀 IP 컬럼의 콤마 구분/줄바꿈 구분/단일 값 포맷을 모두 지원해야 한다."""
    assert parse_excel_ips("10.20.30.40, 50.60.70.80") == ["10.20.30.40", "50.60.70.80"]
    assert parse_excel_ips("10.20.30.40\n50.60.70.80") == ["10.20.30.40", "50.60.70.80"]
    assert parse_excel_ips("10.20.30.40") == ["10.20.30.40"]


def test_hostname_min_length_filters_short_names():
    """
    호스트명이 MIN_HOSTNAME_LEN(4) 미만이면 오매칭 방지를 위해 호스트명 검색 자체를
    건너뛴다. 짧은 이름이 티켓 본문에 실제로 등장해도 매칭되면 안 된다.
    """
    assert len("web") < MIN_HOSTNAME_LEN  # 전제 확인

    items = [{"no": "ITEM-1", "ip": "", "hostname": "web"}]
    tickets = [{"key": "TICKET-1", "summary": "web 서버 점검", "description": ""}]

    result = match_items_by_ip(items, tickets)

    assert "ITEM-1" not in result["matched"]
    assert result["unmatched"] == items


def test_hostname_case_insensitive_matching():
    """티켓 본문이 대문자여도 항목의 소문자 호스트명과 매칭되어야 한다 (양쪽 다 lower() 적용)."""
    items = [{"no": "ITEM-1", "ip": "", "hostname": "web-server-01"}]
    tickets = [{"key": "TICKET-1", "summary": "WEB-SERVER-01 점검작업", "description": ""}]

    result = match_items_by_ip(items, tickets)

    assert "ITEM-1" in result["matched"]
    assert result["matched"]["ITEM-1"][0]["key"] == "TICKET-1"


def test_ip_priority_before_hostname_supplement():
    """
    IP로 매칭된 티켓과, IP로는 못 잡았지만 호스트명으로 잡히는 다른 티켓이 있으면
    둘 다 found 목록에 포함되어야 한다 (호스트명 매칭은 "보강"이지 "대체"가 아님).
    """
    items = [{"no": "ITEM-1", "ip": "10.20.30.40", "hostname": "web-server-02"}]
    tickets = [
        {"key": "TICKET-A", "summary": "IP 10.20.30.40 변경", "description": ""},
        {"key": "TICKET-B", "summary": "web-server-02 재부팅", "description": ""},
    ]

    result = match_items_by_ip(items, tickets)

    matched_keys = {t["key"] for t in result["matched"]["ITEM-1"]}
    assert matched_keys == {"TICKET-A", "TICKET-B"}


def test_duplicate_ticket_not_double_counted():
    """
    같은 티켓이 IP로도 걸리고 호스트명으로도 걸리는 경우, found 목록에는
    한 번만 나타나야 한다 (seen 집합을 통한 key 기준 중복 제거).
    """
    items = [{"no": "ITEM-1", "ip": "10.20.30.40", "hostname": "web-server-03"}]
    tickets = [
        {"key": "TICKET-A", "summary": "IP 10.20.30.40 web-server-03 변경", "description": ""},
    ]

    result = match_items_by_ip(items, tickets)

    found = result["matched"]["ITEM-1"]
    assert len(found) == 1
    assert len(found) == len({t["key"] for t in found})


def test_cmdb_key_exact_match():
    """티켓의 cmdb_keys 필드에 항목 번호가 포함되면 정확히 매칭되어야 한다 (EoS 전용)."""
    tickets = [{"key": "TICKET-A", "cmdb_keys": ["ASSET-123", "ASSET-456"]}]
    items = [{"no": "ASSET-123"}]

    result = match_items_by_cmdb_key(items, tickets)

    assert result["ASSET-123"] == [tickets[0]]


def test_cmdb_key_no_match_returns_absent():
    """
    매칭되는 cmdb_keys가 없으면 해당 item_no는 반환 dict에 아예 존재하지 않아야 한다
    (빈 리스트도 아니고, key 자체가 없음 - `if found:` 가드 때문).
    """
    tickets = [{"key": "TICKET-A", "cmdb_keys": ["ASSET-123"]}]
    items = [{"no": "ASSET-999"}]

    result = match_items_by_cmdb_key(items, tickets)

    assert "ASSET-999" not in result


def test_merge_ticket_maps_deduplicates_across_sources():
    """
    두 매칭 소스가 같은 item_no에 대해 겹치는 티켓 key를 갖고 있으면,
    합친 결과에는 겹치는 티켓이 한 번만 남아야 한다.
    """
    ticket_x = {"key": "TICKET-X"}
    ticket_y = {"key": "TICKET-Y"}
    ticket_z = {"key": "TICKET-Z"}

    source_a = {"ITEM-1": [ticket_x, ticket_y]}
    source_b = {"ITEM-1": [ticket_y, ticket_z]}

    merged = merge_ticket_maps(source_a, source_b)

    merged_keys = {t["key"] for t in merged["ITEM-1"]}
    assert merged_keys == {"TICKET-X", "TICKET-Y", "TICKET-Z"}
    assert len(merged["ITEM-1"]) == 3


def test_merge_ticket_maps_different_items_both_kept():
    """서로 다른 item_no를 가진 두 매칭 소스를 합치면 둘 다 결과에 남아야 한다."""
    source_a = {"ITEM-1": [{"key": "TICKET-A"}]}
    source_b = {"ITEM-2": [{"key": "TICKET-B"}]}

    merged = merge_ticket_maps(source_a, source_b)

    assert merged["ITEM-1"] == [{"key": "TICKET-A"}]
    assert merged["ITEM-2"] == [{"key": "TICKET-B"}]
