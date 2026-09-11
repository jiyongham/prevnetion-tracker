# app/services/capacity_data.py
"""
용량관리 대시보드/포털 홈 공용 외부 데이터 수집 (dr_data.py/eos_data.py와 같은 역할·설계).

DR/EoS는 진작에 stale-while-revalidate 캐시가 있는데 용량관리만 없어서, 대시보드
("/capacity")와 포털 홈("/") 양쪽이 매 요청 jira.get_capacity_tickets()를 동기로
새로 불렀다. JIRA가 느려지거나 완전히 끊기면(타임아웃 30초) 이 조회 하나가 요청
스레드를 그만큼 물고 있는데, 포털 홈은 DATA/ARCH 두 시트를 순서대로 조회해서 매
방문마다 이걸 최대 두 번 반복했다 - 첫 화면(포털 홈)이 유독 무겁고 가끔 안 뜨는
것처럼 보인 원인이 여기다.

캐시하는 건 시트 구분 전의 원본 티켓 목록(build_capacity_ticket_summary 결과)뿐이다.
JIRA 조회 자체가 시트 구분 없이 "예방4" 티켓을 통째로 받아오고, 시트별로 나누는 건
매칭(match_items_by_ip) 이후 단계이기 때문이다. 시트별 매칭은 대상 목록(items)이
매 요청 새로 읽히는 것과 맞춰 그때그때 다시 계산한다 - 로컬 연산이라 몇 ms면 끝나고,
관리자가 웹에서 제외/복귀·일정 수정을 해도 즉시 반영돼야 하니 여기는 캐시하지 않는다
(그래서 DR과 달리 exclude API가 이 캐시를 무효화할 필요도 없다).
"""
import logging
import threading
import time

from app.config import settings
from app.core.capacity_loader import load_capacity_items_merged
from app.core.jira_client import jira
from app.services.capacity import build_capacity_ticket_summary, filter_tickets_by_sheet
from app.services.matcher import match_items_by_ip

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300

_cache: dict = {"at": 0.0, "value": None}   # value: (tickets, jira_error) | None(아직 안 채워짐)
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()   # 외부 조회는 동시에 하나만


def invalidate_cache() -> None:
    """엑셀이 바뀌는 것과는 무관하지만(캐시는 JIRA 원본만), 강제 갱신이 필요할 때를 위해 남겨둔다."""
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def cached_at() -> float:
    """마지막 외부 조회 시각 (epoch). 아직 없으면 0"""
    with _cache_lock:
        return _cache["at"]


def _collect_external() -> tuple[list, str | None]:
    try:
        issues = jira.get_capacity_tickets()
        tickets = build_capacity_ticket_summary(issues, settings.planned_end_date_field)
        return tickets, None
    except Exception as e:
        logger.warning(f"용량관리 JIRA 조회 실패 (엑셀 기준으로 계속): {e}")
        return [], str(e)


def _refresh() -> tuple[list, str | None]:
    """외부 조회 후 캐시 갱신. refresh 락을 쥔 상태에서만 부른다."""
    started = time.time()
    value = _collect_external()
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["value"] = value
    logger.info(f"용량관리 외부 데이터 갱신 완료 ({time.time() - started:.1f}초)")
    return value


def _refresh_in_background() -> None:
    """이미 갱신 중이면 아무것도 하지 않는다 (요청마다 스레드가 쌓이지 않도록)."""
    if not _refresh_lock.acquire(blocking=False):
        return

    def run():
        try:
            _refresh()
        except Exception as e:
            # 갱신에 실패해도 화면은 직전 값으로 계속 뜬다. 다음 요청이 다시 시도한다.
            logger.warning(f"용량관리 백그라운드 갱신 실패 (직전 값 유지): {e}")
        finally:
            _refresh_lock.release()

    threading.Thread(target=run, name="capacity-cache-refresh", daemon=True).start()


def get_tickets(use_jira: bool = True, force_refresh: bool = False) -> tuple[list, str | None]:
    """
    반환: (tickets, jira_error). tickets는 시트 구분 전 전체 목록 - 호출 쪽에서
    match_items_by_ip + filter_tickets_by_sheet로 원하는 시트만 걸러 쓴다.

    force_refresh=True면 갱신이 끝날 때까지 기다렸다가 새 값을 준다 (리포트 발송처럼
    최신값이 꼭 필요한 경로용).
    """
    if not use_jira:
        return [], None

    with _cache_lock:
        entry_at, entry_value = _cache["at"], _cache["value"]

    if entry_value is not None and not force_refresh:
        if time.time() - entry_at >= CACHE_TTL_SEC:
            # 낡았지만 그대로 돌려주고 갱신은 뒤에서 (다음 요청부터 새 값)
            _refresh_in_background()
        return entry_value

    # 캐시가 비었거나 강제 갱신: 조회가 끝날 때까지 기다린다.
    # 락을 기다리는 동안 다른 스레드가 갱신을 마쳤다면(= 내가 요청한 시각 이후에 채워졌다면)
    # 같은 조회를 또 하지 않고 그 결과를 쓴다.
    requested_at = time.time()
    with _refresh_lock:
        with _cache_lock:
            entry_at, entry_value = _cache["at"], _cache["value"]
        if entry_value is not None and entry_at >= requested_at:
            return entry_value
        return _refresh()


def get_matched_items(
    sheet: str, use_jira: bool = True, force_refresh: bool = False
) -> tuple[list[dict], dict, str | None]:
    """
    엑셀+DB 병합 + JIRA 티켓 매칭 + 미응답 대상 target 승격까지 한 번에 처리한다.

    대시보드("/capacity")와 리포트(capacity_report.py) 양쪽이 이 로직을 각자 따로
    구현하고 있었는데, "미응답이어도 매칭된 [예방4] 티켓이 있으면 target으로 승격"을
    한쪽에만 넣었더니 리포트 본문의 "증설 예정/미회신 대수"가 계속 승격 전 숫자로
    화면과 어긋났다. 이 함수 하나로 합쳐 양쪽이 항상 같은 결과를 쓰게 한다.

    반환: (items, ticket_map, jira_error). items는 승격 반영 후 전체 목록
    (제외/미응답 포함) - 시트별 대상/제외/미응답 구성은 여기서 이미 확정된다.
    """
    items = load_capacity_items_merged(sheet=sheet)
    ticket_map = {}
    jira_error = None

    if use_jira:
        tickets, jira_error = get_tickets(force_refresh=force_refresh)
        targets = [i for i in items if i["is_target"]]
        match_result = match_items_by_ip(targets, tickets)
        # 같은 서버가 DATA/ARCH 양쪽에 다 있을 수 있어, 변경작업내용으로 이 시트 소속만 남김
        ticket_map = filter_tickets_by_sheet(match_result["matched"], sheet)

        # 미응답(증설 여부 O/X 미기입) 대상도 매칭된 [예방4] 티켓이 있으면 target으로
        # 승격한다. 담당자가 회신 없이 그냥 증설해버리는 경우가 실제로 있어서(예:
        # 메시징서비스 서버) - "회신이 없다"와 "증설을 안 했다"는 다르다. 실제 증설
        # 티켓이 있다는 사실 자체가 증설 여부를 확정하는 근거다. 승격 후엔 다른 target과
        # 완전히 같은 기준(judge_capacity)으로 완료 여부를 판단하므로, 티켓만 있고 아직
        # 진행 중이면 "미완료"로 뜨고 무조건 완료 처리되는 건 아니다.
        no_reply = [i for i in items if i["status_kind"] == "no_reply"]
        if no_reply:
            no_reply_match = match_items_by_ip(no_reply, tickets)["matched"]
            no_reply_ticket_map = filter_tickets_by_sheet(no_reply_match, sheet)
            for item in no_reply:
                matched = no_reply_ticket_map.get(item["no"])
                if matched:
                    item["is_target"] = True
                    item["status_kind"] = "target"
                    ticket_map[item["no"]] = matched

    return items, ticket_map, jira_error


def prewarm() -> None:
    """
    기동 직후 캐시를 미리 채운다 (앱 시작을 막지 않도록 백그라운드 스레드).
    첫 방문자(대부분 포털 홈)가 JIRA 조회를 기다리지 않게 하는 것이 목적이라 실패해도 그냥 넘어간다.
    """
    def run():
        try:
            get_tickets()
        except Exception as e:
            logger.warning(f"용량관리 캐시 예열 실패 (첫 요청 때 다시 시도): {e}")

    threading.Thread(target=run, name="capacity-prewarm", daemon=True).start()
