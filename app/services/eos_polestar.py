# app/services/eos_polestar.py
"""
Polestar CI명의 '_OLD' 접미사로 EoS 실제 전환 완료를 판정한다.

작업이 정상 완료되면 TO-BE 서버는 '_NEW'가 빠져 원래 이름이 되고, AS-IS 서버에는
'_OLD'가 붙는다. 즉 '{시스템명}_OLD' CI가 Polestar에 존재하면 그 대상은 전환 완료다.

JIRA 티켓 기준(변경계획시작일) 판정은 실제 완료를 보장하지 못해 실측치보다 높게
잡히고(계획일만 지나도 완료로 셈), 티켓 status='완료'만 세면 반대로 낮게 잡힌다.
Polestar는 작업자가 거의 반드시 반영하므로 이 판정이 담당자 수기 확인치와 가장 가깝다.

매칭 키가 두 개인 이유:
- 이름: 엑셀 시스템명과 Polestar CI명이 대체로 같지만, '개발서버'/'개발', 관계사
  태그 상이('[공,Nu]'/'[스,Nu]') 등 드리프트가 있어 이름만으로는 놓치는 게 생긴다.
- IP: 엑셀 IP로 Polestar CI를 찾아 '그쪽의 실제 CI명'을 얻으면 이름 드리프트를
  우회할 수 있다. (호스트명은 Polestar 리소스 '설명'란에만 있고 REST API로는
  제공되지 않아 조인 키로 쓸 수 없다.)
둘 중 하나라도 '_OLD'가 확인되면 전환 완료로 본다. 유사도(fuzzy) 매칭은
'APP DB 2호기_OLD'가 'APP DB 4호기'에 붙는 식의 오탐이 나와 쓰지 않는다.
"""
import re

from app.core.polestar_client import polestar

# 접미사 표기가 '_OLD', '_old', '#NEW', '- OLD' 등으로 제각각이라 폭넓게 인식한다
_SUFFIX_RE = re.compile(r"[_#\-\s]?(OLD|NEW)$")


def _norm(name: str) -> str:
    """공백 제거 + 대문자 (표기 흔들림 흡수)"""
    return re.sub(r"\s+", "", (name or "")).upper()


def _basename(name: str) -> str:
    """끝의 _OLD/_NEW 접미사를 뗀 기준명"""
    return _SUFFIX_RE.sub("", _norm(name))


def _suffix(name: str) -> str:
    """'OLD' | 'NEW' | '' """
    m = _SUFFIX_RE.search(_norm(name))
    return m.group(1) if m else ""


def build_index(resources: list[dict]) -> dict:
    """
    Polestar 리소스 목록 -> 조회용 인덱스
    - by_name: 기준명 -> {'OLD'|'NEW'|'': [CI, ...]}
    - by_ip  : IP -> [CI, ...]
    """
    by_name: dict[str, dict[str, list]] = {}
    by_ip: dict[str, list] = {}
    for ci in resources:
        by_name.setdefault(_basename(ci.get("name")), {}).setdefault(_suffix(ci.get("name")), []).append(ci)
        ip = (ci.get("ipAddress") or "").strip()
        if ip and ip != "null":
            by_ip.setdefault(ip, []).append(ci)
    return {"by_name": by_name, "by_ip": by_ip}


def judge_converted(item: dict, index: dict) -> tuple[bool, str]:
    """
    한 EoS 대상의 전환 완료 여부. 반환: (완료여부, 근거)
    근거는 어느 키로 확인했는지 화면/진단에서 보여주기 위한 것.
    """
    by_name, by_ip = index["by_name"], index["by_ip"]
    system_name = item.get("system_name", "")

    # 엑셀 이름 자체가 이미 _OLD면 전환이 끝나 리네임된 것
    if _suffix(system_name) == "OLD":
        return True, "엑셀 CI명 _OLD"

    if "OLD" in by_name.get(_basename(system_name), {}):
        return True, "Polestar CI명 매칭"

    # 이름이 드리프트된 경우: IP로 Polestar의 실제 CI명을 찾아 그 기준으로 재확인
    for ci in by_ip.get((item.get("ip") or "").strip(), []):
        if "OLD" in by_name.get(_basename(ci.get("name")), {}):
            return True, f"IP 경유 매칭 ({ci.get('name')})"

    return False, ""


def judge_pending(item: dict, index: dict) -> bool:
    """전환 전이지만 TO-BE('_NEW')가 이미 준비된 상태인지 (차주 계획 파악 참고용)"""
    by_name, by_ip = index["by_name"], index["by_ip"]
    if "NEW" in by_name.get(_basename(item.get("system_name", "")), {}):
        return True
    return any(
        "NEW" in by_name.get(_basename(ci.get("name")), {})
        for ci in by_ip.get((item.get("ip") or "").strip(), [])
    )


def confirmed_item_nos(items: list[dict], resources: list[dict] | None = None) -> set[str]:
    """
    Polestar에서 '_OLD'가 확인된 대상의 item_no 집합.
    calc_eos_completion(..., polestar_confirmed=...)에 그대로 넘겨 쓴다.

    Polestar 조회는 네트워크 호출이라 대상마다 부르지 않고 목록을 한 번만 받아 인덱싱한다.

    ※ Polestar CI명에는 리네임 시각이 없어 "언제 전환됐는지"를 알 수 없다. 그래서 과거
      시점(as_of)으로 조회하면 그 이후에 전환된 건까지 완료로 잡혀 약간 과대계상된다.
      '오늘 기준' 집계에는 문제없고, 과거 소급 집계에는 JIRA CMDB 근거만 쓰는 게 정확하다.
    """
    if resources is None:
        resources = polestar.list_resources()
    index = build_index(resources)
    return {i["item_no"] for i in items if judge_converted(i, index)[0]}


def check_eos_conversion(items: list[dict], resources: list[dict] | None = None) -> dict:
    """
    EoS 대상들의 Polestar 기준 전환 현황.
    반환: {"converted": [...], "pending_new": [...], "not_started": [...], "unlinked": [...]}
    - unlinked: 이름으로도 IP로도 Polestar에서 CI를 못 찾은 대상 (수동 확인 필요)
    """
    if resources is None:
        resources = polestar.list_resources()
    index = build_index(resources)
    by_name, by_ip = index["by_name"], index["by_ip"]

    result = {"converted": [], "pending_new": [], "not_started": [], "unlinked": []}
    for item in items:
        done, reason = judge_converted(item, index)
        if done:
            result["converted"].append({**item, "polestar_reason": reason})
            continue

        linked = _basename(item.get("system_name", "")) in by_name or (item.get("ip") or "").strip() in by_ip
        if not linked:
            result["unlinked"].append(item)
        elif judge_pending(item, index):
            result["pending_new"].append(item)
        else:
            result["not_started"].append(item)
    return result
