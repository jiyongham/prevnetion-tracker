# app/core/insight_client.py
"""JIRA Insight/Assets(CMDB) 조회. JIRA와 같은 호스트/세션(인증)을 재사용한다."""
import time

from app.core.jira_client import jira

OBJECT_SCHEMA_ID = 401          # SINC Assets Schema NEW (I&C 통합 CMDB)
SERVER_OBJECT_TYPE = "서버"

ATTR_HOSTNAME = 7269
ATTR_STATUS = 7271
ATTR_IP = 7277
ATTR_OPS_TEAM = 12342     # 시스템운영팀
ATTR_OWNERS = 12343       # 시스템담당자 (이름-팀, 다중)

STATUS_ACTIVE = "운영"
OPS_TEAM_PLACEHOLDER = "현업관리"  # 관계사 시스템은 이 더미값만 들어있고 실제 팀은 현업담당자쪽에 있음

# 관계사(운영 주체가 다른 계열사) 시스템은 '서버' 오브젝트의 시스템담당자가 비어있고,
# 대신 별도 오브젝트 타입 '현업담당자'가 '서버'를 참조(인바운드)하는 형태로 관리된다.
BIZ_OWNER_OBJECT_TYPE = "현업담당자"
ATTR_BIZ_NAME = 7763      # 담당자명
ATTR_BIZ_TEAM = 7662      # 팀명 ("점포운영담당 > 운영기획팀" 형태)


def _attr_values(entry: dict, attr_id: int) -> list[dict]:
    for a in entry.get("attributes", []):
        if a.get("objectTypeAttributeId") == attr_id:
            return a.get("objectAttributeValues", [])
    return []


def _attr_value(entry: dict, attr_id: int) -> str:
    vals = _attr_values(entry, attr_id)
    return vals[0]["displayValue"] if vals else ""


def _iql_search(iql: str, retries: int = 2) -> list[dict]:
    """
    Insight AQL 오브젝트 검색. 동시 조회량이 많을 때 서버가 간헐적으로
    빈 응답/일시 오류를 주는 경우가 있어 짧은 재시도를 둔다.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = jira.session.get(
                f"{jira.base_url}/rest/insight/1.0/iql/objects",
                params={"iql": iql, "objectSchemaId": OBJECT_SCHEMA_ID},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json().get("objectEntries", [])
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
    raise last_err


def search_server_by_hostname(hostname: str) -> list[dict]:
    """호스트명으로 CMDB '서버' 오브젝트 조회 (중복 등록분 전부 반환)"""
    if not hostname:
        return []
    hostname_escaped = hostname.replace('"', '\\"')
    iql = f'objectType="{SERVER_OBJECT_TYPE}" AND "호스트명"="{hostname_escaped}"'
    return _iql_search(iql)


def search_business_owners(server_object_key: str) -> list[dict]:
    """
    '서버'를 참조하는 '현업담당자' 오브젝트 조회 (관계사 시스템 담당자).
    시스템담당자(12343)가 비어있는 관계사 서버는 이쪽에 실제 담당자가 들어있다.
    """
    if not server_object_key:
        return []
    key_escaped = server_object_key.replace('"', '\\"')
    iql = f'objectType="{BIZ_OWNER_OBJECT_TYPE}" AND "서버"="{key_escaped}"'

    owners = []
    for e in _iql_search(iql):
        name = _attr_value(e, ATTR_BIZ_NAME)
        if not name:
            continue
        team_full = _attr_value(e, ATTR_BIZ_TEAM)
        team = team_full.rsplit(">", 1)[-1].strip() if team_full else ""
        owners.append({
            "raw": f"{name}-{team}" if team else name,
            "name": name,
            "team": team,
            "team_full": team_full,
            "source": "현업담당자",
        })
    return owners


def get_server_asset(hostname: str) -> dict | None:
    """
    호스트명으로 CMDB 서버 자산 조회. 동일 호스트명이 여러 건(구/신)이면
    상태가 '운영'인 것을 우선 사용 (없으면 가장 최근 갱신분).
    '시스템담당자'(I&C 직접관리)에 '현업담당자'(관계사 업무 담당자, 인바운드 참조)를 합쳐 반환한다.
    """
    entries = search_server_by_hostname(hostname)
    if not entries:
        return None

    active = [e for e in entries if _attr_value(e, ATTR_STATUS) == STATUS_ACTIVE]
    picked = active[0] if active else max(entries, key=lambda e: e.get("updated") or "")
    object_key = picked.get("objectKey", "")

    owners = [
        {"raw": v["displayValue"], "source": "시스템담당자", **_split_owner(v["displayValue"])}
        for v in _attr_values(picked, ATTR_OWNERS)
    ]
    ops_team = _attr_value(picked, ATTR_OPS_TEAM)

    # 관계사 시스템은 시스템담당자가 비어있거나 팀이 더미값('현업관리')인 경우가 많아,
    # 그럴 때만 인바운드 참조(현업담당자)를 추가 조회한다 (불필요한 API 호출 절감)
    biz_owners = []
    if not owners or not ops_team or ops_team == OPS_TEAM_PLACEHOLDER:
        biz_owners = search_business_owners(object_key)

    seen_raw = {o["raw"] for o in owners}
    for bo in biz_owners:
        if bo["raw"] not in seen_raw:
            owners.append(bo)
            seen_raw.add(bo["raw"])
    if (not ops_team or ops_team == OPS_TEAM_PLACEHOLDER) and biz_owners:
        ops_team = biz_owners[0]["team"] or ops_team

    return {
        "object_key": object_key,
        "status": _attr_value(picked, ATTR_STATUS),
        "ip": _attr_value(picked, ATTR_IP),
        "ops_team": ops_team,
        "owners": owners,
        "duplicate_count": len(entries),
    }


def _split_owner(raw: str) -> dict:
    """'홍길동-포털서비스팀' -> {name, team}"""
    if "-" in raw:
        name, team = raw.split("-", 1)
    else:
        name, team = raw, ""
    return {"name": name.strip(), "team": team.strip()}
