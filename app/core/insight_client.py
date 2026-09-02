# app/core/insight_client.py
"""JIRA Insight/Assets(CMDB) 조회. JIRA와 같은 호스트/세션(인증)을 재사용한다."""
import concurrent.futures
import logging
import time

from app.core.jira_client import jira

logger = logging.getLogger(__name__)

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
ATTR_BIZ_SERVER = 7652    # 담당 '서버' 참조 (한 담당자가 여러 서버를 가짐)


def _attr_values(entry: dict, attr_id: int) -> list[dict]:
    for a in entry.get("attributes", []):
        if a.get("objectTypeAttributeId") == attr_id:
            return a.get("objectAttributeValues", [])
    return []


def _attr_value(entry: dict, attr_id: int) -> str:
    vals = _attr_values(entry, attr_id)
    return vals[0]["displayValue"] if vals else ""


# 한 번에 받아올 오브젝트 수. 지정하지 않으면 서버가 25건만 주고 잘라버린다.
RESULT_PER_PAGE = 200
# IN 절 한 묶음에 넣을 호스트명 수 (IQL 길이와 응답 크기 사이의 절충)
IQL_IN_CHUNK = 50
# 묶음 병렬 조회 수 (jira 세션 커넥션 풀 20 이내)
MAX_LOOKUP_WORKERS = 8


def _iql_page(iql: str, page: int, retries: int = 2) -> dict:
    """Insight AQL 한 페이지. 동시 조회량이 많을 때 서버가 간헐적으로
    빈 응답/일시 오류를 주는 경우가 있어 짧은 재시도를 둔다."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = jira.session.get(
                f"{jira.base_url}/rest/insight/1.0/iql/objects",
                params={
                    "iql": iql,
                    "objectSchemaId": OBJECT_SCHEMA_ID,
                    "resultPerPage": RESULT_PER_PAGE,
                    "page": page,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
    raise last_err


def _iql_search(iql: str) -> list[dict]:
    """Insight AQL 오브젝트 검색 (페이징 포함)."""
    entries: list[dict] = []
    page = 1
    while True:
        data = _iql_page(iql, page)
        entries += data.get("objectEntries", [])
        # toIndex: 이번 페이지까지 받은 마지막 순번, totalFilterCount: 조건에 맞는 전체 건수
        if data.get("toIndex", 0) >= data.get("totalFilterCount", 0) or not data.get("objectEntries"):
            return entries
        page += 1


def _quote(value: str) -> str:
    return '"' + (value or "").replace('"', '\\"') + '"'


def _iql_in_chunks(field: str, object_type: str, values: list[str]) -> list[dict]:
    """
    objectType="..." AND "field" IN (...) 을 묶음으로 나눠 병렬 조회.

    호스트명 하나마다 따로 부르면 387대 기준 CMDB 조회에만 35초가 걸렸다.
    IN 절로 묶으면 요청 수가 1/50로 줄고, 묶음끼리는 서로 의존이 없어 병렬로 부를 수 있다.
    """
    chunks = [values[i:i + IQL_IN_CHUNK] for i in range(0, len(values), IQL_IN_CHUNK)]
    if not chunks:
        return []

    def fetch(chunk: list[str]) -> list[dict]:
        iql = f'objectType={_quote(object_type)} AND {_quote(field)} IN ({",".join(_quote(v) for v in chunk)})'
        try:
            return _iql_search(iql)
        except Exception as e:
            logger.warning(f"CMDB 묶음 조회 실패 ({object_type} {len(chunk)}건): {e}")
            return []

    entries: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_LOOKUP_WORKERS) as ex:
        for part in ex.map(fetch, chunks):
            entries += part
    return entries


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

    return [o for o in (_biz_owner(e) for e in _iql_search(iql)) if o]


def _biz_owner(entry: dict) -> dict | None:
    """'현업담당자' 오브젝트 -> 담당자 dict ('점포운영담당 > 운영기획팀'에서 팀명만 취한다)"""
    name = _attr_value(entry, ATTR_BIZ_NAME)
    if not name:
        return None
    team_full = _attr_value(entry, ATTR_BIZ_TEAM)
    team = team_full.rsplit(">", 1)[-1].strip() if team_full else ""
    return {
        "raw": f"{name}-{team}" if team else name,
        "name": name,
        "team": team,
        "team_full": team_full,
        "source": "현업담당자",
    }


def _pick_entry(entries: list[dict]) -> dict:
    """동일 호스트명이 여러 건(구/신)이면 상태가 '운영'인 것 우선, 없으면 가장 최근 갱신분"""
    active = [e for e in entries if _attr_value(e, ATTR_STATUS) == STATUS_ACTIVE]
    return active[0] if active else max(entries, key=lambda e: e.get("updated") or "")


def _build_asset(entries: list[dict], biz_owners: list[dict]) -> dict:
    """'서버' 오브젝트 + 관계사 '현업담당자'를 합쳐 자산 정보 한 건으로"""
    picked = _pick_entry(entries)
    owners = [
        {"raw": v["displayValue"], "source": "시스템담당자", **_split_owner(v["displayValue"])}
        for v in _attr_values(picked, ATTR_OWNERS)
    ]
    ops_team = _attr_value(picked, ATTR_OPS_TEAM)

    seen_raw = {o["raw"] for o in owners}
    for bo in biz_owners:
        if bo["raw"] not in seen_raw:
            owners.append(bo)
            seen_raw.add(bo["raw"])
    if (not ops_team or ops_team == OPS_TEAM_PLACEHOLDER) and biz_owners:
        ops_team = biz_owners[0]["team"] or ops_team

    return {
        "object_key": picked.get("objectKey", ""),
        "status": _attr_value(picked, ATTR_STATUS),
        "ip": _attr_value(picked, ATTR_IP),
        "ops_team": ops_team,
        "owners": owners,
        "duplicate_count": len(entries),
    }


def _needs_biz_owners(entries: list[dict]) -> bool:
    """관계사 시스템은 시스템담당자가 비어있거나 팀이 더미값('현업관리')인 경우가 많다.
    그럴 때만 인바운드 참조(현업담당자)를 추가 조회한다 (불필요한 API 호출 절감)."""
    picked = _pick_entry(entries)
    ops_team = _attr_value(picked, ATTR_OPS_TEAM)
    return not _attr_values(picked, ATTR_OWNERS) or not ops_team or ops_team == OPS_TEAM_PLACEHOLDER


def get_server_asset(hostname: str) -> dict | None:
    """
    호스트명으로 CMDB 서버 자산 조회 (한 건).
    '시스템담당자'(I&C 직접관리)에 '현업담당자'(관계사 업무 담당자, 인바운드 참조)를 합쳐 반환한다.
    """
    entries = search_server_by_hostname(hostname)
    if not entries:
        return None
    biz = (
        search_business_owners(_pick_entry(entries).get("objectKey", ""))
        if _needs_biz_owners(entries) else []
    )
    return _build_asset(entries, biz)


def get_server_assets(hostnames: list[str]) -> dict[str, dict]:
    """
    호스트명 여러 개를 한 번에 조회. 반환: {호스트명(소문자): 자산}

    한 건씩 부르던 걸 IN 절로 묶는다 (387대 기준 35초 -> 3초). 대소문자 표기가
    CMDB와 엑셀에서 다를 수 있어 소문자 키로 돌려주고, 호출부도 소문자로 찾는다.
    """
    wanted = [h for h in {(h or "").strip() for h in hostnames} if h]
    if not wanted:
        return {}

    by_host: dict[str, list[dict]] = {}
    for e in _iql_in_chunks("호스트명", SERVER_OBJECT_TYPE, wanted):
        host = _attr_value(e, ATTR_HOSTNAME).strip().lower()
        if host:
            by_host.setdefault(host, []).append(e)

    # 현업담당자 보강이 필요한 서버들만 다시 한 묶음으로
    need_keys = {
        _pick_entry(entries).get("objectKey", ""): host
        for host, entries in by_host.items() if _needs_biz_owners(entries)
    }
    biz_by_host: dict[str, list[dict]] = {}
    if need_keys:
        for e in _iql_in_chunks("서버", BIZ_OWNER_OBJECT_TYPE, [k for k in need_keys if k]):
            owner = _biz_owner(e)
            if not owner:
                continue
            for v in _attr_values(e, ATTR_BIZ_SERVER):
                ref = (v.get("referencedObject") or {}).get("objectKey", "")
                host = need_keys.get(ref)
                if host:
                    biz_by_host.setdefault(host, []).append(owner)

    return {host: _build_asset(entries, biz_by_host.get(host, [])) for host, entries in by_host.items()}


def _split_owner(raw: str) -> dict:
    """'홍길동-포털서비스팀' -> {name, team}"""
    if "-" in raw:
        name, team = raw.split("-", 1)
    else:
        name, team = raw, ""
    return {"name": name.strip(), "team": team.strip()}
