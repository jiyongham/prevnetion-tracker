# app/services/eos_data.py
"""
EoS 대시보드/리포트 공용 데이터 수집.

대시보드(routes/eos.py)와 주간 리포트(eos_report.py)가 같은 수집 로직을 각자
구현하고 있어 한쪽만 고치면 숫자가 어긋나던 걸 여기로 합쳤다.

느린 건 외부 호출이다 (측정치: JIRA 티켓 3초 + IP/호스트명 매칭 2초 + Polestar 1.4초).
엑셀+DB 병합 항목(items)은 매번 새로 읽는다 - 관리자가 완료 체크나 담당자를 수정하면
즉시 화면에 반영돼야 하기 때문 - 대신 외부 유래 결과만 캐시한다.

캐시해도 안전한 이유: ticket_map/polestar_confirmed는 item_no·IP·호스트명·시스템명으로
만들어지는데, DB 병합(load_eos_items_merged)이 덮어쓰는 건 일정/완료표기/담당자/비고뿐이라
매칭 키는 바뀌지 않는다.

갱신 방식은 stale-while-revalidate다. TTL이 지나도 일단 있는 값을 바로 돌려주고
갱신은 뒤에서 돌린다. 만료 시점에 들어온 요청이 갱신을 다 기다리던 구조에서는
5분에 한 번씩 누군가가 화면 앞에서 수십 초를 기다렸고, 그 사이 들어온 요청들이
저마다 같은 JIRA 조회를 중복으로 시작했다. 기다리는 건 캐시가 아예 비어 있는
첫 요청뿐이고, 그것도 기동 직후 prewarm()이 미리 채워두면 없다.
"""
import logging
import threading
import time

from app.config import settings
from app.core.eos_loader import load_eos_items_merged
from app.core.jira_client import jira
from app.models.db import get_eos_polestar_seen, record_eos_polestar_seen
from app.services.eos import build_eos_ticket_summary
from app.services.eos_polestar import confirmed_reasons
from app.services.matcher import match_items_by_cmdb_key, match_items_by_ip, merge_ticket_maps

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300

_cache: dict = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()      # 캐시 읽기/쓰기 (짧게만 잡는다)
_refresh_lock = threading.Lock()    # 외부 조회는 동시에 하나만 (같은 조회 중복 방지)


def invalidate_cache() -> None:
    """엑셀 재업로드 등으로 대상 목록 자체가 바뀌었을 때 강제 갱신용"""
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def cached_at() -> float:
    """마지막 외부 조회 시각 (epoch). 아직 없으면 0"""
    with _cache_lock:
        return _cache["at"]


def polestar_error() -> str | None:
    """마지막 조회에서의 Polestar 오류 (정상이면 None). 화면에 조회 실패를 알리는 용도"""
    with _cache_lock:
        value = _cache["value"]
    return value[3] if value else None


def _collect_external(targets: list[dict]) -> tuple[dict, dict[str, dict], str | None, str | None]:
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

    current = None
    p_error = None
    try:
        current = confirmed_reasons(targets)
        added = record_eos_polestar_seen(current)
        if added:
            logger.info(f"Polestar '_OLD' 신규 확인 {added}건 기록")
    except Exception as e:
        p_error = str(e)
        logger.warning(f"Polestar 조회 실패 (기록된 관측으로 계속): {e}")

    return ticket_map, merge_polestar_latch(current), jira_error, p_error


def merge_polestar_latch(current: dict[str, str] | None) -> dict[str, dict]:
    """
    이번 조회 결과 + DB에 남은 과거 관측을 합친다. 반환: {item_no: {reason, first_seen, last_seen, present}}

    전환이 끝난 AS-IS 서버는 결국 폐기(CI 삭제)되는데, 판정은 매번 '지금 상태'를 다시
    보기 때문에 그 순간 완료 근거가 사라져 완료 대수가 뒤로 간다. 그래서 한 번 확인한
    '_OLD'는 기록으로 남겨 계속 근거로 인정하고, 지금은 안 보인다는 사실(present)만
    따로 표시한다. Polestar 조회 자체가 실패해도 기록분은 그대로 유효하다.
    """
    stored = get_eos_polestar_seen()
    merged = {}
    for item_no, row in stored.items():
        merged[item_no] = {
            "reason": row["reason"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            # 조회에 실패했으면(current is None) 사라졌는지 알 수 없다. '폐기'로 표시하면
            # Polestar 장애 한 번에 전 건이 폐기된 것처럼 보이므로 모른다=그대로 둔다.
            "present": True if current is None else (item_no in current),
        }
    return merged


def _refresh(targets: list[dict]) -> tuple[dict, dict[str, dict], str | None, str | None]:
    """외부 조회 후 캐시 갱신. _refresh_lock을 쥔 상태에서만 부른다."""
    started = time.time()
    value = _collect_external(targets)
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["value"] = value
    logger.info(f"EoS 외부 데이터 갱신 완료 ({time.time() - started:.1f}초)")
    return value


def _refresh_in_background(targets: list[dict]) -> None:
    """이미 갱신 중이면 아무것도 하지 않는다 (요청마다 스레드가 쌓이지 않도록)."""
    if not _refresh_lock.acquire(blocking=False):
        return

    def run():
        try:
            _refresh(targets)
        except Exception as e:
            # 갱신에 실패해도 화면은 직전 값으로 계속 뜬다. 다음 요청이 다시 시도한다.
            logger.warning(f"EoS 외부 데이터 백그라운드 갱신 실패 (직전 값 유지): {e}")
        finally:
            _refresh_lock.release()

    threading.Thread(target=run, name="eos-cache-refresh", daemon=True).start()


def get_eos_data(use_external: bool = True, force_refresh: bool = False):
    """
    반환: (items, ticket_map, polestar_confirmed, jira_error)

    use_external=False면 외부 호출 없이 엑셀+DB만 (오프라인/테스트용).
    force_refresh=True면 갱신이 끝날 때까지 기다렸다가 새 값을 준다 (리포트 발송처럼
    최신값이 꼭 필요한 경로용).
    """
    items = load_eos_items_merged()
    if not use_external:
        return items, {}, None, None

    with _cache_lock:
        cached, cached_time = _cache["value"], _cache["at"]

    if cached is not None and not force_refresh:
        if time.time() - cached_time >= CACHE_TTL_SEC:
            # 낡았지만 그대로 돌려주고 갱신은 뒤에서 (다음 요청부터 새 값)
            _refresh_in_background([i for i in items if i["is_target"]])
        ticket_map, polestar_confirmed, jira_error, _ = cached
        return items, ticket_map, polestar_confirmed, jira_error

    # 캐시가 비었거나 강제 갱신: 조회가 끝날 때까지 기다린다.
    # 락을 기다리는 동안 다른 스레드가 갱신을 마쳤다면(= 내가 요청한 시각 이후에 채워졌다면)
    # 같은 조회를 또 하지 않고 그 결과를 쓴다.
    requested_at = time.time()
    with _refresh_lock:
        with _cache_lock:
            cached, cached_time = _cache["value"], _cache["at"]
        if cached is not None and cached_time >= requested_at:
            value = cached
        else:
            value = _refresh([i for i in items if i["is_target"]])

    ticket_map, polestar_confirmed, jira_error, _ = value
    return items, ticket_map, polestar_confirmed, jira_error


def prewarm() -> None:
    """
    기동 직후 캐시를 미리 채운다 (앱 시작을 막지 않도록 백그라운드 스레드).
    첫 방문자가 JIRA/Polestar 조회를 기다리지 않게 하는 것이 목적이라 실패해도 그냥 넘어간다.
    """
    def run():
        try:
            get_eos_data()
        except Exception as e:
            logger.warning(f"EoS 캐시 예열 실패 (첫 요청 때 다시 시도): {e}")

    threading.Thread(target=run, name="eos-prewarm", daemon=True).start()
