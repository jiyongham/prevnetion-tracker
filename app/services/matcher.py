# app/services/matcher.py
import bisect
import re

# IP 패턴 (경계 포함 - 부분일치 방지)
IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def extract_ips(text: str) -> set[str]:
    """텍스트에서 IP 주소 모두 추출"""
    if not text:
        return set()
    return set(IP_PATTERN.findall(text))


def parse_excel_ips(ip_value: str) -> list[str]:
    """
    엑셀 IP 컬럼 파싱
    '10.1.1.1' / '10.1.1.1, 10.1.1.2' / '10.1.1.1\n10.1.1.2' 대응
    """
    if not ip_value:
        return []
    return IP_PATTERN.findall(ip_value)


# 호스트명 오매칭 방지 최소 길이
MIN_HOSTNAME_LEN = 4


def _ticket_text(t: dict) -> str:
    """매칭용 텍스트 (변경작업 대상 등 포함). 없으면 제목+본문."""
    return t.get("match_text") or f"{t.get('summary', '')}\n{t.get('description') or ''}"


def build_ip_index(tickets: list[dict]) -> dict[str, list[dict]]:
    """티켓 매칭 텍스트에서 IP 추출해 인덱스 생성 {ip: [ticket, ...]}"""
    index: dict[str, list[dict]] = {}
    for t in tickets:
        for ip in extract_ips(_ticket_text(t)):
            index.setdefault(ip, []).append(t)
    return index


def build_hostname_index(tickets: list[dict]) -> dict:
    """
    호스트명 검색용 인덱스.

    티켓마다 `host in text`를 도는 방식은 대상 수 x 티켓 수만큼 파이썬 호출이 생기고
    (EoS 기준 387 x 1,173 = 45만 회) 매번 티켓 본문 전체(합계 12MB)를 훑어 8초가 걸렸다.
    본문을 공백으로 쪼개 중복을 없애면 같은 내용이 12MB -> 1MB로 줄어드는데,
    (변경 티켓 본문에는 표 머리글·안내 문구 같은 같은 말이 계속 반복된다)
    호스트명은 공백을 포함하지 않으므로 이 축약본만 훑어도 결과는 동일하다.

    반환: {"tokens": [토큰...], "corpus": NUL로 이은 토큰들, "starts": 토큰 시작 offset,
           "by_token": {토큰: [티켓 index...]}}
    """
    by_token: dict[str, list[int]] = {}
    for n, t in enumerate(tickets):
        for token in set(_ticket_text(t).lower().split()):
            by_token.setdefault(token, []).append(n)

    tokens = list(by_token)
    starts: list[int] = []
    pos = 0
    for token in tokens:
        starts.append(pos)
        pos += len(token) + 1
    # 구분자로 NUL을 쓰는 이유: 호스트명에 절대 들어가지 않는 문자라 두 토큰에
    # 걸친 문자열이 호스트명으로 잘못 매칭될 수 없다.
    return {"tokens": tokens, "corpus": "\u0000".join(tokens), "starts": starts, "by_token": by_token}


def find_hostname_tickets(host: str, index: dict, tickets: list[dict]) -> list[int]:
    """호스트명이 등장하는 티켓 index 목록 (티켓 순서).

    호스트명에 공백이 있으면 축약본으로는 찾을 수 없으므로 원문을 그대로 훑는다
    (엑셀 오타 등으로 드물게 생긴다)."""
    if not host or host.split() != [host]:
        return [n for n, t in enumerate(tickets) if host and host in _ticket_text(t).lower()]

    corpus, starts, tokens, by_token = (
        index["corpus"], index["starts"], index["tokens"], index["by_token"]
    )
    hits: set[int] = set()
    pos = corpus.find(host)
    while pos != -1:
        hits.update(by_token[tokens[bisect.bisect_right(starts, pos) - 1]])
        pos = corpus.find(host, pos + len(host))
    return sorted(hits)


def match_items_by_ip(items: list[dict], tickets: list[dict]) -> dict:
    """
    엑셀 항목 <-> JIRA 티켓 매칭 (IP 우선, 실패 시 호스트명).
    티켓의 '변경작업 대상' 필드까지 포함해 매칭한다.

    반환: {
        "matched": {item_no: [ticket, ...]},   # 걸린 티켓 전부 (반기/종류 선택은 judge)
        "unmatched": [item, ...],
        "ip_index_size": int
    }
    """
    ip_index = build_ip_index(tickets)
    host_index = build_hostname_index(tickets)
    host_cache: dict[str, list[int]] = {}   # 같은 호스트명을 쓰는 항목이 있어 한 번만 훑는다

    matched = {}
    unmatched = []

    for item in items:
        found = []
        seen = set()

        # 1) IP 매칭
        for ip in parse_excel_ips(item.get("ip", "")):
            for t in ip_index.get(ip, []):
                if t.get("key") not in seen:
                    seen.add(t["key"])
                    found.append(t)

        # 2) 호스트명 매칭 (IP로 못 잡은 티켓 보강)
        host = (item.get("hostname") or "").strip().lower()
        if len(host) >= MIN_HOSTNAME_LEN:
            if host not in host_cache:
                host_cache[host] = find_hostname_tickets(host, host_index, tickets)
            for idx in host_cache[host]:
                t = tickets[idx]
                if t.get("key") not in seen:
                    seen.add(t["key"])
                    found.append(t)

        if found:
            matched[item["no"]] = found
        else:
            unmatched.append(item)

    return {
        "matched": matched,
        "unmatched": unmatched,
        "ip_index_size": len(ip_index),
    }


def match_items_by_cmdb_key(items: list[dict], tickets: list[dict]) -> dict[str, list[dict]]:
    """
    티켓의 '작업 완료(CMDB)' 필드(cmdb_keys, Insight Key 집합)로 직접 매칭 (EoS 전용).
    변경작업내용에 호스트명/IP가 안 적혀 있어도, 이 필드에 대상 CMDB Key가 있으면
    IP/호스트명 매칭 없이도 정확히 연결된다.
    """
    key_index: dict[str, list[dict]] = {}
    for t in tickets:
        for key in t.get("cmdb_keys") or ():
            key_index.setdefault(key, []).append(t)

    matched = {}
    for item in items:
        found = key_index.get(item["no"])
        if found:
            matched[item["no"]] = found
    return matched


def merge_ticket_maps(*maps: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """여러 매칭 소스(ticket_map)를 item_no 기준으로 합친다 (같은 티켓 key는 중복 제거)"""
    merged: dict[str, list[dict]] = {}
    for m in maps:
        for item_no, tickets in m.items():
            bucket = merged.setdefault(item_no, [])
            seen = {t["key"] for t in bucket}
            for t in tickets:
                if t["key"] not in seen:
                    seen.add(t["key"])
                    bucket.append(t)
    return merged
