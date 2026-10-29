# app/services/dr_match.py
"""
DR훈련 티켓 <-> 대상 매칭.

DR은 티켓 본문의 IP/호스트명 텍스트가 아니라, 티켓의 작업 대상 CMDB 필드
(settings.dr_cmdb_target_field, "CI명 (ASSET-xxxxx)" 형태)로만 대상과 연결한다.
텍스트 매칭은 변경작업 대상에 IP가 빠졌거나 'ecvan'처럼 약칭만 적힌 티켓을 놓쳤다.

대상(엑셀 행)에는 CMDB Key가 없으므로 호스트명으로 CMDB '서버' 자산을 찾아 Key와
CI명을 얻는다 (owner_check.lookup_cmdb_assets - 호스트명 단위 캐시를 같이 쓴다).
  1) CMDB Key 일치
  2) CI명 일치 - 대상의 CMDB CI명 또는 엑셀 호스트명과 대소문자만 무시한 완전 일치.
     Key가 없는 항목이나, 같은 호스트명이 구/신 자산으로 중복 등록돼 티켓이 다른 Key를
     가리키는 경우를 보강한다. 부분 일치는 다른 서버까지 붙일 수 있어 보지 않는다.

대시보드(dr_data)·리포트(report)·담당자 확인(owner_check)이 모두 이 모듈만 거쳐야
화면마다 완료 수가 어긋나지 않는다.
"""
import logging
import re

from app.config import settings
from app.services.completion import build_ticket_summary

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


def match_dr_tickets(targets: list[dict], tickets: list[dict]) -> dict[str, list[dict]]:
    """반환: {item_no: [ticket, ...]} - 걸린 티켓 전부 (반기/종류 선택은 completion.judge)"""
    if not settings.dr_cmdb_target_field:
        # 조용히 0건으로 매칭되면 완료율이 통째로 떨어진 채 리포트가 나간다 - 오류로 드러낸다.
        raise ValueError("DR_CMDB_TARGET_FIELD가 설정되지 않아 DR 티켓을 대상과 연결할 수 없습니다")

    by_key: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for t in tickets:
        for target in t.get("cmdb_targets") or ():
            if target["key"]:
                by_key.setdefault(target["key"], []).append(t)
            if target["name"]:
                by_name.setdefault(target["name"].lower(), []).append(t)

    # owner_check가 이 모듈을 쓰므로 순환 import를 피해 함수 안에서 가져온다.
    from app.services.owner_check import lookup_cmdb_assets

    assets = lookup_cmdb_assets(targets)
    unresolved = [i["no"] for i in targets if i["no"] not in assets]
    if unresolved:
        logger.info(f"DR 대상 {len(unresolved)}건은 CMDB 자산을 못 찾아 CI명(호스트명)으로만 매칭합니다: {unresolved[:10]}")

    matched: dict[str, list[dict]] = {}
    for item in targets:
        asset = assets.get(item["no"]) or {}
        found = list(by_key.get(asset.get("object_key") or "", []))
        names = {(asset.get("label") or "").strip().lower(), (item.get("hostname") or "").strip().lower()}
        for name in names - {""}:
            found += by_name.get(name, [])
        if found:
            matched[item["no"]] = list({t["key"]: t for t in found}.values())
    return matched
