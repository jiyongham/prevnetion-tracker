# app/services/owner_check.py
"""
조직변경(팀명 변경 등)으로 엑셀 담당자 정보가 실제와 달라졌을 가능성이 있는
대상을 찾아준다. 두 가지 근거를 사용한다.

1) CMDB(Insight) '서버' 자산의 시스템운영팀/시스템담당자 — 호스트명으로 조회.
   동일 호스트명이 구/신으로 중복 등록된 경우 상태가 '운영'인 자산을 우선한다.
2) 해당 시스템에 매칭된 JIRA 티켓 중 가장 최근 티켓의 'JSM요청자'
   (customfield_11500, "이름(비고) - 팀명" 형식).

수정은 자동으로 하지 않고, 검토용 후보 목록만 만든다 (원본은 엑셀).
"""
import concurrent.futures

from app.config import settings
from app.core.capacity_loader import get_targets as get_capacity_targets
from app.core.capacity_loader import load_capacity_items_merged
from app.core.eos_loader import get_targets as get_eos_targets
from app.core.eos_loader import load_eos_items_merged
from app.core.excel_loader import get_targets, load_dr_items_merged, scope_h2_targets
from app.core.insight_client import get_server_asset
from app.core.jira_client import jira
from app.services.capacity import build_capacity_ticket_summary, filter_tickets_by_sheet
from app.services.completion import build_ticket_summary
from app.services.eos import build_eos_ticket_summary
from app.services.matcher import match_items_by_cmdb_key, match_items_by_ip, merge_ticket_maps
from app.services.reminder import clean_name, parse_owners


def parse_jsm_requester(display_name: str) -> dict:
    """'홍길동(비고) - OO팀' -> {name: 홍길동, team: OO팀}"""
    if not display_name:
        return {"name": "", "team": ""}
    if " - " in display_name:
        name_part, team = display_name.rsplit(" - ", 1)
    else:
        name_part, team = display_name, ""
    return {"name": clean_name(name_part), "team": team.strip()}


def collect_targets_with_tickets(half: str, use_jira: bool = True):
    """대상 목록 + (윈도우 제한 없는) 매칭 티켓맵"""
    items = load_dr_items_merged(half=half)
    if half == "H2":
        items = scope_h2_targets(items)
    targets = get_targets(items)

    ticket_map = {}
    jira_error = None
    if use_jira:
        try:
            issues = jira.get_dr_tickets()
            tickets = build_ticket_summary(issues, settings.planned_end_date_field)
            match_result = match_items_by_ip(targets, tickets)
            ticket_map = match_result["matched"]
        except Exception as e:
            jira_error = str(e)

    return targets, ticket_map, jira_error


def collect_eos_targets_with_tickets(use_jira: bool = True):
    """EoS 대상 목록 + (윈도우 제한 없는) 매칭 티켓맵"""
    items = load_eos_items_merged()
    targets = get_eos_targets(items)

    ticket_map = {}
    jira_error = None
    if use_jira:
        try:
            issues = jira.get_eos_tickets()
            tickets = build_eos_ticket_summary(issues, settings.planned_end_date_field)
            cmdb_map = match_items_by_cmdb_key(targets, tickets)
            ip_map = match_items_by_ip(targets, tickets)["matched"]
            ticket_map = merge_ticket_maps(cmdb_map, ip_map)
        except Exception as e:
            jira_error = str(e)

    return targets, ticket_map, jira_error


def collect_capacity_targets_with_tickets(sheet: str, use_jira: bool = True):
    """용량관리 대상 목록 + (윈도우 제한 없는) 매칭 티켓맵 (시트별 - DATA/ARCH)"""
    items = load_capacity_items_merged(sheet=sheet)
    targets = get_capacity_targets(items)

    ticket_map = {}
    jira_error = None
    if use_jira:
        try:
            issues = jira.get_capacity_tickets()
            tickets = build_capacity_ticket_summary(issues, settings.planned_end_date_field)
            match_result = match_items_by_ip(targets, tickets)
            ticket_map = filter_tickets_by_sheet(match_result["matched"], sheet)
        except Exception as e:
            jira_error = str(e)

    return targets, ticket_map, jira_error


def lookup_cmdb_assets(targets: list[dict]) -> dict[str, dict]:
    """item_no -> CMDB 서버 자산 (호스트명 기준, 병렬 조회)"""
    have_host = [i for i in targets if i.get("hostname")]

    def _fetch(item):
        try:
            return item["no"], get_server_asset(item["hostname"])
        except Exception as e:
            print(f"⚠️ CMDB 조회 실패 (NO {item['no']}, {item['hostname']}): {e}")
            return item["no"], None

    results: dict[str, dict] = {}
    if not have_host:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for no, asset in ex.map(_fetch, have_host):
            if asset:
                results[no] = asset
    return results


def find_owner_mismatches(targets: list[dict], ticket_map: dict) -> list[dict]:
    """
    CMDB 시스템운영팀 및/또는 JIRA 티켓 JSM요청자 팀이, 현재 엑셀 담당자들의
    팀 어디에도 없는 대상을 '불일치 후보'로 반환 (둘 중 하나만 걸려도 포함).

    담당자 칸 자체에 팀 정보가 전혀 없는 행(예: "이름-팀" 포맷이 아니라 이름만
    콤마로 나열된 경우)은 비교 기준(current_teams)이 비어서 항상 불일치로 오판되므로,
    그런 행은 애초에 비교 대상에서 제외한다 (팀 정보 누락이지 실제 불일치가 아님).
    """
    cmdb_map = lookup_cmdb_assets(targets)

    candidates = []
    for item in targets:
        owners = parse_owners(item.get("owner", ""))
        current_teams = {o["team"] for o in owners if o["team"]}
        if not current_teams:
            continue

        row = {
            "no": item["no"],
            "item_no": item.get("item_no", item["no"]),  # 저장용 키 (용량관리는 "DATA:5"처럼 시트 접두어 포함)
            "system_name": item.get("system_name") or item.get("ci_name", ""),
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "current_owner": item.get("owner", ""),
            "cmdb_status": "", "cmdb_ops_team": "", "cmdb_owners": "",
            "cmdb_key": "", "cmdb_dup": 0, "cmdb_mismatch": False,
            "jsm_requester_name": "", "jsm_requester_team": "",
            "jira_key": "", "ticket_created": None, "jira_mismatch": False,
        }

        # 1) CMDB 근거
        asset = cmdb_map.get(item["no"])
        if asset and asset["ops_team"]:
            row.update({
                "cmdb_status": asset["status"],
                "cmdb_ops_team": asset["ops_team"],
                "cmdb_owners": " || ".join(o["raw"] for o in asset["owners"]),
                "cmdb_key": asset["object_key"],
                "cmdb_dup": asset["duplicate_count"],
            })
            if asset["ops_team"] not in current_teams:
                row["cmdb_mismatch"] = True

        # 2) JIRA JSM요청자 근거
        tickets = ticket_map.get(item["no"]) or []
        with_requester = [t for t in tickets if t.get("jsm_requester")]
        if with_requester:
            latest = max(with_requester, key=lambda t: t.get("created") or "")
            requester = parse_jsm_requester(latest["jsm_requester"])
            if requester["team"]:
                row.update({
                    "jsm_requester_name": requester["name"],
                    "jsm_requester_team": requester["team"],
                    "jira_key": latest["key"],
                    "ticket_created": latest.get("created_date"),
                })
                if requester["team"] not in current_teams:
                    row["jira_mismatch"] = True

        if row["cmdb_mismatch"] or row["jira_mismatch"]:
            candidates.append(row)

    return candidates
