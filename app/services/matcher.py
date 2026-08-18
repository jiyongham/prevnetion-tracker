# app/services/matcher.py
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
    # 호스트명 매칭용: 티켓별 소문자 텍스트 미리 계산
    ticket_texts = [(t, _ticket_text(t).lower()) for t in tickets]

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
            for t, text in ticket_texts:
                if t.get("key") not in seen and host in text:
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
