# app/services/dr_data.py
"""
DR훈련 대시보드/리마인드/담당자확인 공용 외부 데이터 수집 (EoS의 eos_data와 같은 역할).

측정치: JIRA 티켓 조회 2.4초 + IP/호스트명 매칭 0.2초 + CMDB 조회. 대시보드, 리마인드,
담당자 확인이 각자 이걸 매번 다시 했다. 화면이 그대로인 동안 클릭이 안 먹은 것처럼
보이고, 그 사이 다른 걸 누르면 첫 요청이 취소돼 엉뚱한 화면으로 갔다.

캐시해도 안전한 이유: ticket_map은 item_no·IP·호스트명으로 만들어지는데, DB 병합
(load_dr_items_merged)이 덮어쓰는 건 일정/완료표기/담당자/비고뿐이라 매칭 키는 안 바뀐다.
그래서 items는 매번 새로 읽고(관리자 수정이 즉시 반영돼야 함) 외부 유래 결과만 캐시한다.

ticket_map은 수행방식(mode) 필터를 적용하기 전 전체 대상 기준으로 만든다. 매핑은
item_no 기준이라 어떤 모드로 걸러도 조회 결과가 같기 때문에 모드별로 따로 캐시할 필요가 없다.

갱신 방식은 stale-while-revalidate다 (eos_data와 동일). TTL이 지나도 있는 값을 바로
돌려주고 갱신은 뒤에서 돌린다. 만료 시점에 들어온 요청이 갱신을 다 기다리던 구조에서는
5분에 한 번씩 누군가가 화면 앞에서 몇 초를 기다렸고, 그 사이 들어온 요청들이 저마다
같은 JIRA 조회를 중복으로 시작했다. 기다리는 건 캐시가 아예 비어 있는 첫 요청뿐이고,
그것도 기동 직후 prewarm()이 미리 채워두면 없다.
"""
import logging
import threading
import time

from app.config import settings
from app.core.excel_loader import load_dr_items_merged, scope_h2_targets
from app.core.jira_client import jira
from app.services.completion import build_ticket_summary
from app.services.matcher import match_items_by_ip

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300

_cache: dict = {}                   # half -> {"at": float, "value": (ticket_map, jira_error)}
_cache_lock = threading.Lock()      # 캐시 읽기/쓰기 (짧게만 잡는다)
_refresh_locks: dict[str, threading.Lock] = {}   # 반기별 외부 조회는 동시에 하나만


def _refresh_lock(half: str) -> threading.Lock:
    with _cache_lock:
        return _refresh_locks.setdefault(half, threading.Lock())


def invalidate_cache(half: str | None = None) -> None:
    """엑셀 재업로드 등으로 대상 목록 자체가 바뀌었을 때 강제 갱신용"""
    with _cache_lock:
        if half:
            _cache.pop(half, None)
        else:
            _cache.clear()


def cached_at(half: str) -> float:
    """해당 반기의 마지막 외부 조회 시각 (epoch). 아직 없으면 0"""
    with _cache_lock:
        return (_cache.get(half) or {}).get("at", 0.0)


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


def _refresh(half: str, targets: list[dict]) -> tuple[dict, str | None]:
    """외부 조회 후 캐시 갱신. 해당 반기의 refresh 락을 쥔 상태에서만 부른다."""
    started = time.time()
    value = _collect_external(targets)
    with _cache_lock:
        _cache[half] = {"at": time.time(), "value": value}
    logger.info(f"DR훈련({half}) 외부 데이터 갱신 완료 ({time.time() - started:.1f}초)")
    return value


def _refresh_in_background(half: str, targets: list[dict]) -> None:
    """이미 갱신 중이면 아무것도 하지 않는다 (요청마다 스레드가 쌓이지 않도록)."""
    lock = _refresh_lock(half)
    if not lock.acquire(blocking=False):
        return

    def run():
        try:
            _refresh(half, targets)
        except Exception as e:
            # 갱신에 실패해도 화면은 직전 값으로 계속 뜬다. 다음 요청이 다시 시도한다.
            logger.warning(f"DR훈련({half}) 백그라운드 갱신 실패 (직전 값 유지): {e}")
        finally:
            lock.release()

    threading.Thread(target=run, name=f"dr-cache-refresh-{half}", daemon=True).start()


def get_ticket_map(
    half: str, items: list[dict], use_jira: bool = True, force_refresh: bool = False
) -> tuple[dict, str | None]:
    """
    반환: (ticket_map, jira_error)

    force_refresh=True면 갱신이 끝날 때까지 기다렸다가 새 값을 준다 (리포트 발송처럼
    최신값이 꼭 필요한 경로용).
    """
    if not use_jira:
        return {}, None

    with _cache_lock:
        entry = _cache.get(half)

    if entry and not force_refresh:
        if time.time() - entry["at"] >= CACHE_TTL_SEC:
            # 낡았지만 그대로 돌려주고 갱신은 뒤에서 (다음 요청부터 새 값)
            _refresh_in_background(half, [i for i in items if i["is_target"]])
        return entry["value"]

    # 캐시가 비었거나 강제 갱신: 조회가 끝날 때까지 기다린다.
    # 락을 기다리는 동안 다른 스레드가 갱신을 마쳤다면(= 내가 요청한 시각 이후에 채워졌다면)
    # 같은 조회를 또 하지 않고 그 결과를 쓴다.
    requested_at = time.time()
    with _refresh_lock(half):
        with _cache_lock:
            entry = _cache.get(half)
        if entry and entry["at"] >= requested_at:
            return entry["value"]
        return _refresh(half, [i for i in items if i["is_target"]])


def prewarm(half: str) -> None:
    """
    기동 직후 캐시를 미리 채운다 (앱 시작을 막지 않도록 백그라운드 스레드).
    첫 방문자가 JIRA 조회를 기다리지 않게 하는 것이 목적이라 실패해도 그냥 넘어간다.
    """
    def run():
        try:
            get_ticket_map(half, load_items(half))
        except Exception as e:
            logger.warning(f"DR훈련({half}) 캐시 예열 실패 (첫 요청 때 다시 시도): {e}")

    threading.Thread(target=run, name=f"dr-prewarm-{half}", daemon=True).start()
