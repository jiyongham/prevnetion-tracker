# app/services/eos_data.py
"""
EoS 대시보드/리포트 공용 데이터 수집.

대시보드(routes/eos.py)와 주간 리포트(eos_report.py)가 같은 수집 로직을 각자
구현하고 있어 한쪽만 고치면 숫자가 어긋나던 걸 여기로 합쳤다.

느린 건 외부 호출이다 (측정치: JIRA 티켓 23초 + IP/호스트명 매칭 11초 = 요청당 약 40초).
매 페이지 로드마다 이걸 반복해서 /eos 진입이 버벅였다. 그래서 외부 유래 결과만
TTL 캐시하고, 엑셀+DB 병합 항목(items)은 매번 새로 읽는다 - 관리자가 완료 체크나
담당자를 수정하면 즉시 화면에 반영돼야 하기 때문.

캐시해도 안전한 이유: ticket_map/polestar_confirmed는 item_no·IP·호스트명·시스템명으로
만들어지는데, DB 병합(load_eos_items_merged)이 덮어쓰는 건 일정/완료표기/담당자/비고뿐이라
매칭 키는 바뀌지 않는다.
"""
import logging
import time

from app.config import settings
from app.core.eos_loader import load_eos_items_merged
from app.core.jira_client import jira
from app.services.eos import build_eos_ticket_summary
from app.services.eos_polestar import confirmed_item_nos
from app.services.matcher import match_items_by_cmdb_key, match_items_by_ip, merge_ticket_maps

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300

_cache: dict = {"at": 0.0, "value": None}


def invalidate_cache() -> None:
    """엑셀 재업로드 등으로 대상 목록 자체가 바뀌었을 때 강제 갱신용"""
    _cache["at"] = 0.0
    _cache["value"] = None


def _collect_external(targets: list[dict]) -> tuple[dict, set[str] | None, str | None]:
    """JIRA 티켓 매칭 + Polestar 전환 확인. 어느 한쪽이 실패해도 나머지로 계속 진행한다."""
    ticket_map: dict = {}
    jira_error = None
    try:
        issues = jira.get_eos_tickets()
        tickets = build_eos_ticket_summary(issues, settings.planned_end_date_field)
        # 작업 완료(CMDB) 필드의 Insight Key로 우선 매칭, 그 필드가 없는 티켓은 IP/호스트명으로 보강
        cmdb_map = match_items_by_cmdb_key(targets, tickets)
        ip_map = match_items_by_ip(targets, tickets)["matched"]
        ticket_map = merge_ticket_maps(cmdb_map, ip_map)
    except Exception as e:
        jira_error = str(e)
        logger.warning(f"EoS JIRA 조회 실패 (엑셀 기준으로 계속): {e}")

    polestar_confirmed = None
    try:
        polestar_confirmed = confirmed_item_nos(targets)
    except Exception as e:
        logger.warning(f"Polestar 조회 실패 (JIRA CMDB 근거만으로 계속): {e}")

    return ticket_map, polestar_confirmed, jira_error


def get_eos_data(use_external: bool = True, force_refresh: bool = False):
    """
    반환: (items, ticket_map, polestar_confirmed, jira_error)

    use_external=False면 외부 호출 없이 엑셀+DB만 (오프라인/테스트용).
    """
    items = load_eos_items_merged()
    if not use_external:
        return items, {}, None, None

    fresh = _cache["value"] is not None and (time.time() - _cache["at"]) < CACHE_TTL_SEC
    if fresh and not force_refresh:
        ticket_map, polestar_confirmed, jira_error = _cache["value"]
        return items, ticket_map, polestar_confirmed, jira_error

    value = _collect_external([i for i in items if i["is_target"]])
    _cache["at"] = time.time()
    _cache["value"] = value
    ticket_map, polestar_confirmed, jira_error = value
    return items, ticket_map, polestar_confirmed, jira_error
