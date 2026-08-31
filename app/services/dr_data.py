# app/services/dr_data.py
"""
DR훈련 대시보드/리마인드/담당자확인 공용 외부 데이터 수집 (EoS의 eos_data와 같은 역할).

측정치: JIRA 티켓 조회 2.6초 + IP/호스트명 매칭 3.6초 + CMDB 조회 2.2초 = 요청당 약 8.5초.
대시보드, 미계획 리마인드, 담당자 확인이 각자 이걸 매번 다시 했다. 8초간 화면이 그대로라
클릭이 안 먹은 것처럼 보이고, 그 사이 다른 걸 누르면 첫 요청이 취소돼 엉뚱한 화면으로 갔다.

캐시해도 안전한 이유: ticket_map은 item_no·IP·호스트명으로 만들어지는데, DB 병합
(load_dr_items_merged)이 덮어쓰는 건 일정/완료표기/담당자/비고뿐이라 매칭 키는 안 바뀐다.
그래서 items는 매번 새로 읽고(관리자 수정이 즉시 반영돼야 함) 외부 유래 결과만 캐시한다.

ticket_map은 수행방식(mode) 필터를 적용하기 전 전체 대상 기준으로 만든다. 매핑은
item_no 기준이라 어떤 모드로 걸러도 조회 결과가 같기 때문에 모드별로 따로 캐시할 필요가 없다.
"""
import logging
import time

from app.config import settings
from app.core.excel_loader import load_dr_items_merged, scope_h2_targets
from app.core.jira_client import jira
from app.services.completion import build_ticket_summary
from app.services.matcher import match_items_by_ip

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300

_cache: dict = {}  # half -> {"at": float, "value": (ticket_map, jira_error)}


def invalidate_cache(half: str | None = None) -> None:
    """엑셀 재업로드 등으로 대상 목록 자체가 바뀌었을 때 강제 갱신용"""
    if half:
        _cache.pop(half, None)
    else:
        _cache.clear()


def load_items(half: str) -> list[dict]:
    """엑셀+DB 병합 항목 (하반기는 상반기 무중단 대상으로 한정). 캐시하지 않는다."""
    items = load_dr_items_merged(half=half)
    return scope_h2_targets(items) if half == "H2" else items


def _collect_external(targets: list[dict]) -> tuple[dict, str | None]:
    try:
        issues = jira.get_dr_tickets()
        tickets = build_ticket_summary(issues, settings.planned_end_date_field)
        return match_items_by_ip(targets, tickets)["matched"], None
    except Exception as e:
        logger.warning(f"DR훈련 JIRA 조회 실패 (엑셀 기준으로 계속): {e}")
        return {}, str(e)


def get_ticket_map(
    half: str, items: list[dict], use_jira: bool = True, force_refresh: bool = False
) -> tuple[dict, str | None]:
    """반환: (ticket_map, jira_error)"""
    if not use_jira:
        return {}, None

    entry = _cache.get(half)
    if entry and not force_refresh and time.time() - entry["at"] < CACHE_TTL_SEC:
        return entry["value"]

    value = _collect_external([i for i in items if i["is_target"]])
    _cache[half] = {"at": time.time(), "value": value}
    return value
