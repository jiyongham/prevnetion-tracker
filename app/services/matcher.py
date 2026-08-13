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


def build_ip_index(tickets: list[dict]) -> dict[str, list[dict]]:
    """
    티켓의 제목+본문에서 IP 추출해서 인덱스 생성
    {ip: [ticket, ...]}
    """
    index: dict[str, list[dict]] = {}

    for t in tickets:
        text = f"{t.get('summary', '')}\n{t.get('description') or ''}"
        for ip in extract_ips(text):
            index.setdefault(ip, []).append(t)

    return index


def match_items_by_ip(items: list[dict], tickets: list[dict]) -> dict:
    """
    엑셀 항목 <-> JIRA 티켓 IP 매칭

    반환: {
        "matched": {item_no: ticket},
        "unmatched": [item, ...],
        "ip_index_size": int
    }
    """
    ip_index = build_ip_index(tickets)
    matched = {}
    unmatched = []

    for item in items:
        ips = parse_excel_ips(item.get("ip", ""))
        found = None

        for ip in ips:
            candidates = ip_index.get(ip)
            if candidates:
                # 여러 티켓이 걸리면 완료일이 가장 이른 것 선택
                found = sorted(
                    candidates,
                    key=lambda t: t.get("planned_end_date") or __import__("datetime").date.max,
                )[0]
                break

        if found:
            matched[item["no"]] = found
        else:
            unmatched.append(item)

    return {
        "matched": matched,
        "unmatched": unmatched,
        "ip_index_size": len(ip_index),
    }
