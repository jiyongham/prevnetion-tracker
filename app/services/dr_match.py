# app/services/dr_match.py
"""
DR훈련 티켓 <-> 대상 매칭. 아래 두 결과의 합집합이다 (EoS와 같은 방식).

  1) CMDB Key - 티켓의 작업 대상 CMDB 필드(settings.dr_cmdb_target_field,
     "CI명 (ASSET-xxxxx)" 형태)의 Key. 변경작업 대상에 IP가 빠졌거나 'ecvan'처럼
     약칭만 적힌 티켓도 자산만 연결돼 있으면 잡힌다. 대상(엑셀 행)에는 Key가 없으므로
     호스트명으로 CMDB '서버' 자산을 찾아 Key를 얻는다 (owner_check.lookup_cmdb_assets
     - 호스트명 단위 캐시를 같이 쓴다). CI명은 표기가 제각각이라 매칭에 쓰지 않는다.
  2) IP/호스트명 - 제목·본문·변경작업 대상 텍스트 (matcher.match_items_by_ip).
     작업 대상 필드를 비워 둔 티켓을 놓치지 않기 위해 같이 본다.

대시보드(dr_data)·리포트(report)·담당자 확인(owner_check)이 모두 이 모듈만 거쳐야
화면마다 완료 수가 어긋나지 않는다.
"""
import logging
import re

from app.config import settings
from app.services.completion import build_ticket_summary
from app.services.matcher import match_items_by_ip, merge_ticket_maps

logger = logging.getLogger(__name__)

_KEY_IN_PARENS = re.compile(r"\(([A-Z][A-Z0-9]*-\d+)\)")
_SEPARATORS = " \t\r\n,;"


def _parse_text(text: str) -> list[dict]:
    """'CI명 (ASSET-1), CI명2 (ASSET-2)' -> 항목 목록. 괄호 속 Key 앞까지가 그 항목의 CI명이다
    ('AP #1 (Active)'처럼 Key가 아닌 괄호는 CI명에 남는다)."""
    found = []
    start = 0
    for m in _KEY_IN_PARENS.finditer(text):
        found.append({"name": text[start:m.start()].strip(_SEPARATORS), "key": m.group(1)})
        start = m.end()
    rest = text[start:].strip(_SEPARATORS)
    if rest:
        found.append({"name": rest, "key": ""})
    return found


def parse_cmdb_targets(raw) -> list[dict]:
    """작업 대상 CMDB 필드 값 -> [{"name": CI명, "key": CMDB Key}, ...]

    문자열 목록("CI명 (ASSET-1)")이 일반적이지만 Insight 객체 목록({"label", "objectKey"})과
    한 문자열에 여러 항목이 이어진 형태도 받는다."""
    if not raw:
        return []
    targets: list[dict] = []
    for entry in raw if isinstance(raw, list) else [raw]:
        if isinstance(entry, dict):
            name = str(entry.get("label") or entry.get("name") or entry.get("value") or "").strip()
            key = str(entry.get("objectKey") or "").strip()
            if key:
                targets.append({"name": name, "key": key})
                continue
            entry = name
        targets.extend(_parse_text(str(entry)))
    return targets


def build_dr_ticket_summary(issues: list[dict]) -> list[dict]:
    """build_ticket_summary + 작업 대상 CMDB 항목(cmdb_targets)"""
    tickets = build_ticket_summary(issues, settings.planned_end_date_field)
    field = settings.dr_cmdb_target_field
    for ticket, issue in zip(tickets, issues):
        ticket["cmdb_targets"] = parse_cmdb_targets(issue["fields"].get(field)) if field else []
    return tickets


def match_by_cmdb_key(targets: list[dict], tickets: list[dict]) -> dict[str, list[dict]]:
    """작업 대상 CMDB 필드의 Key로 매칭. 반환: {item_no: [ticket, ...]}"""
    by_key: dict[str, list[dict]] = {}
    for t in tickets:
        for target in t.get("cmdb_targets") or ():
            if target["key"]:
                by_key.setdefault(target["key"], []).append(t)
    if not by_key:
        return {}

    # owner_check가 이 모듈을 쓰므로 순환 import를 피해 함수 안에서 가져온다.
    from app.services.owner_check import lookup_cmdb_assets

    assets = lookup_cmdb_assets(targets)
    unresolved = [i["no"] for i in targets if i["no"] not in assets]
    if unresolved:
        logger.info(f"DR 대상 {len(unresolved)}건은 CMDB 자산을 못 찾아 IP/호스트명으로만 매칭합니다: {unresolved[:10]}")

    matched: dict[str, list[dict]] = {}
    for item in targets:
        key = (assets.get(item["no"]) or {}).get("object_key")
        if key and key in by_key:
            matched[item["no"]] = by_key[key]
    return matched


def match_dr_tickets(targets: list[dict], tickets: list[dict]) -> dict[str, list[dict]]:
    """반환: {item_no: [ticket, ...]} - 걸린 티켓 전부 (반기/종류 선택은 completion.judge)"""
    if not settings.dr_cmdb_target_field:
        logger.warning("DR_CMDB_TARGET_FIELD 미설정 - DR 티켓을 IP/호스트명으로만 매칭합니다")
    key_map = match_by_cmdb_key(targets, tickets)
    ip_map = match_items_by_ip(targets, tickets)["matched"]
    return merge_ticket_maps(key_map, ip_map)
