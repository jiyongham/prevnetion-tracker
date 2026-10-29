# app/services/kernel_patched.py
"""커널 패치 완료 근거 수집 (웹 그리드 우선, REST/엑셀 폴백)."""
import logging
import json
import os
import re
import threading
import time
from pathlib import Path

import pandas as pd
import requests

from app.config import settings
from app.core.polestar_client import polestar

logger = logging.getLogger(__name__)

_HOST_HEADERS = {"호스트명", "hostname", "hostnme", "host", "서버명", "name", "리소스명", "리소스이름"}
_IP_HEADERS = {"ip", "ip주소", "ipaddress", "아이피"}
_LEVEL_HEADERS = {
    "os패치레벨", "ospatchlevel", "패치레벨", "patchlevel",
    "커널버전", "커널", "kernel", "kernelversion", "kernelrelease", "versios", "version",
}
_IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_NULL_TEXTS = {"", "null", "none", "nan", "nat", "<na>"}

# REST/엑셀 폴백 캐시. 만료 순간 몰린 요청이 각자 조회하지 않게 갱신은 한 번만 한다.
_CACHE_TTL_SEC = 300
_cache: dict = {"key": None, "at": 0.0, "value": None}
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()


def _norm_header(h) -> str:
    return str(h or "").replace(" ", "").strip().lower()


def _pick_column(df: pd.DataFrame, headers: set[str]) -> str | None:
    for c in df.columns:
        if _norm_header(c) in headers:
            return c
    return None


def _clean_text(value) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        raise ValueError("expected a scalar patch field")
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NULL_TEXTS else text


def _empty(source: str = "", source_kind: str = "") -> dict:
    return {
        "hosts": set(),
        "ips": set(),
        "levels": {},
        "rows": 0,
        "source": source,
        "has_level": False,
        "source_kind": source_kind,
    }


def _copy_source(source: dict) -> dict:
    return {
        "hosts": set(source.get("hosts", set())),
        "ips": set(source.get("ips", set())),
        "levels": dict(source.get("levels", {})),
        "rows": int(source.get("rows", 0) or 0),
        "grid_rows": int(source.get("grid_rows", 0) or 0),
        "grid_pages": int(source.get("grid_pages", 0) or 0),
        "rows_with_level": int(source.get("rows_with_level", 0) or 0),
        "rows_with_ip": int(source.get("rows_with_ip", 0) or 0),
        "ip_conflicts": int(source.get("ip_conflicts", 0) or 0),
        "source": source.get("source", ""),
        "has_level": bool(source.get("has_level")),
        "source_kind": source.get("source_kind", ""),
    }


def _remember_level(levels: dict[str, str], conflicts: set[str], key: str, level: str) -> None:
    if not key or key in conflicts:
        return
    existing = levels.get(key)
    if existing is None:
        levels[key] = level
    elif existing != level:
        conflicts.add(key)
        levels.pop(key, None)


def _cache_key() -> tuple:
    raw_path = settings.kernel_patched_export_path
    mtime = None
    if raw_path:
        p = Path(raw_path)
        if p.exists():
            mtime = p.stat().st_mtime_ns
    # 웹 그리드 결과는 이 캐시에 넣지 않는다 (_web_state) - 폴백 소스만 키에 넣는다.
    return (settings.kernel_polestar_view, raw_path, mtime, polestar.base_url)


def invalidate_patched_source_cache() -> None:
    with _cache_lock:
        _cache["key"] = None
        _cache["at"] = 0.0
        _cache["value"] = None
    with _web_state_lock:
        _web_state.update(key=None, fetched_at=0.0, attempt_at=0.0, value=None, loaded=False)


def read_patched_export(path: str | None = None) -> dict:
    raw = path or settings.kernel_patched_export_path
    if not raw:
        return _empty()
    p = Path(raw)
    if not p.exists():
        return _empty()

    try:
        df = pd.read_excel(p, dtype=str)
    except ImportError as e:
        logger.error(f"패치 확인 파일을 읽지 못했습니다 ({p.name}): {e} — .xlsx 로 저장하거나 xlrd 설치 필요")
        return _empty(source=p.name, source_kind="excel")

    host_col = _pick_column(df, _HOST_HEADERS)
    ip_col = _pick_column(df, _IP_HEADERS)
    level_col = _pick_column(df, _LEVEL_HEADERS)

    hosts: set[str] = set()
    ips: set[str] = set()
    levels: dict[str, str] = {}

    for _, row in df.iterrows():
        level = _clean_text(row[level_col]) if level_col else ""
        if level_col and not level:
            continue

        if host_col:
            host = _clean_text(row[host_col]).lower()
            if host:
                hosts.add(host)
                if level_col:
                    levels[host] = level

        if ip_col:
            found = _IP_PATTERN.findall(_clean_text(row[ip_col]))
            ips.update(found)
            if level_col:
                levels.update({ip: level for ip in found})

    if not host_col and not ip_col:
        logger.warning(f"패치 확인 파일에서 호스트명/IP 컬럼을 못 찾았습니다 ({p.name}). 컬럼: {list(df.columns)[:10]}")
    if not level_col:
        logger.info(f"패치 확인 파일에 OS 패치 레벨 컬럼이 없습니다 ({p.name}) - 목록에 있으면 완료로 봅니다. 컬럼: {list(df.columns)[:10]}")

    return {
        "hosts": hosts,
        "ips": ips,
        "levels": levels,
        "rows": len(df),
        "source": p.name,
        "has_level": bool(level_col),
        "source_kind": "excel",
    }


def read_patched_rest(view: str | None = None) -> dict:
    view_name = (view or settings.kernel_polestar_view or "").strip()
    if not view_name:
        return _empty()
    if not polestar.base_url:
        raise ValueError("polestar_url not configured")

    resp = polestar.session.get(
        f"{polestar.base_url}/rest/measure/vt/{view_name}/all",
        headers={"Accept": "application/json"},
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()

    if not isinstance(payload, dict) or payload.get("ErrorMessage"):
        raise ValueError("malformed Polestar payload")
    data = payload.get("data")
    rows = data.get("measurement") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("malformed Polestar payload")

    host_levels: dict[str, str] = {}
    ip_levels: dict[str, str] = {}
    host_conflicts: set[str] = set()
    ip_conflicts: set[str] = set()
    valid_rows = 0

    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("malformed Polestar payload")
        row = {_norm_header(k): v for k, v in raw.items()}
        level = ""
        for key in ("patchlevel", "versios", "version"):
            level = _clean_text(row.get(key))
            if level:
                break
        if not level:
            continue

        host = _clean_text(row.get("hostname")).lower()
        ips = _IP_PATTERN.findall(_clean_text(row.get("ipaddress")))
        if not host and not ips:
            continue

        valid_rows += 1
        if host:
            _remember_level(host_levels, host_conflicts, host, level)
        for ip in ips:
            _remember_level(ip_levels, ip_conflicts, ip, level)

    return {
        "hosts": set(host_levels),
        "ips": set(ip_levels),
        "levels": {**host_levels, **ip_levels},
        "rows": valid_rows,
        "source": f"Polestar REST {view_name}",
        "has_level": True,
        "source_kind": "api",
    }


def _web_configured() -> bool:
    return bool(settings.kernel_polestar_web_user and settings.kernel_polestar_web_password)


def _read_web_source() -> dict | None:
    try:
        from app.services.kernel_polestar_web import read_patched_web_grid

        web = read_patched_web_grid()
    except Exception as e:
        logger.warning("Polestar 웹 그리드 조회 실패 - 다음 패치 소스로 폴백합니다: %s", type(e).__name__)
        return None
    if web["rows"]:
        return web
    logger.warning("Polestar 웹 그리드에 유효한 패치 레벨 행이 없어 다음 소스로 폴백합니다")
    return None


def _read_fallback_source() -> dict:
    view_name = (settings.kernel_polestar_view or "").strip()
    if view_name:
        try:
            rest = read_patched_rest(view_name)
        except (requests.RequestException, requests.exceptions.JSONDecodeError, ValueError) as e:
            logger.warning(f"커널 패치 Polestar REST 조회 실패 - 엑셀로 폴백합니다: {e}")
        else:
            if rest["rows"]:
                return rest
            logger.warning("커널 패치 Polestar REST에 유효한 버전 행이 없어 엑셀로 폴백합니다")
    return read_patched_export()


# --- 웹 그리드 스냅샷 ---------------------------------------------------------
# 웹 그리드는 Chromium으로 전 페이지를 순회해 느리고 메모리를 많이 쓴다. 화면 요청에서
# 직접 돌리지 않고 백그라운드 스레드에서만 수집하며, 결과는 파일로 남겨 재기동 후에도
# 바로 쓴다. 패치 레벨은 하루에 몇 번 바뀌는 값이라 1시간 주기면 충분하다.
_WEB_TTL_SEC = 3600
_WEB_RETRY_SEC = 600
_web_state: dict = {"key": None, "fetched_at": 0.0, "attempt_at": 0.0, "value": None, "loaded": False}
_web_state_lock = threading.Lock()
_web_refresh_lock = threading.Lock()


def _web_key() -> list:
    # 비밀번호(또는 그 해시)는 디스크에 남기지 않는다 - 계정·주소·경로만으로 구분한다.
    return [
        settings.kernel_polestar_web_user,
        polestar.base_url,
        settings.kernel_polestar_web_login_path,
        settings.kernel_polestar_web_grid_path,
    ]


def _web_snapshot_path() -> Path:
    return Path(settings.db_path).with_name("kernel_web_grid.json")


def _load_web_snapshot_from_disk() -> None:
    """프로세스당 한 번, 저장된 마지막 수집 결과를 메모리로 올린다. _web_state_lock 안에서 부른다."""
    _web_state["loaded"] = True
    path = _web_snapshot_path()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        source = raw["source"]
        source["hosts"] = set(source.get("hosts", []))
        source["ips"] = set(source.get("ips", []))
        _web_state.update(key=raw["key"], fetched_at=float(raw["fetched_at"]), value=_copy_source(source))
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning("저장된 Polestar 웹 그리드 결과를 읽지 못했습니다 (%s): %s", path.name, type(e).__name__)


def _store_web(source: dict) -> None:
    key = _web_key()
    fetched_at = time.time()
    value = _copy_source(source)
    with _web_state_lock:
        _web_state.update(key=key, fetched_at=fetched_at, value=value, loaded=True)

    path = _web_snapshot_path()
    serializable = {**value, "hosts": sorted(value["hosts"]), "ips": sorted(value["ips"])}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"key": key, "fetched_at": fetched_at, "source": serializable}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Polestar 웹 그리드 결과를 저장하지 못했습니다 (%s): %s", path.name, type(e).__name__)


def _fetch_and_store_web() -> dict | None:
    """웹 그리드를 수집해 저장한다. 호출부가 _web_refresh_lock을 잡고 부른다 -
    Chromium은 무슨 경로로든 한 번에 하나만 뜬다."""
    with _web_state_lock:
        _web_state["attempt_at"] = time.time()
    web = _read_web_source()
    if web is not None:
        _store_web(web)
        logger.info("Polestar 웹 그리드 수집 완료: %d행", web["rows"])
    return web


def _refresh_web_worker() -> None:
    try:
        _fetch_and_store_web()
    finally:
        _web_refresh_lock.release()


def _start_web_refresh() -> None:
    """백그라운드 수집을 시작한다. 이미 돌고 있으면 아무것도 하지 않는다."""
    if not _web_refresh_lock.acquire(blocking=False):
        return
    try:
        threading.Thread(target=_refresh_web_worker, name="kernel-web-grid", daemon=True).start()
    except Exception:
        _web_refresh_lock.release()
        raise


def _refresh_web_now() -> dict | None:
    """동기 수집. 백그라운드 수집이 돌고 있으면 끝날 때까지 기다린 뒤 다시 수집한다."""
    with _web_refresh_lock:
        return _fetch_and_store_web()


def _web_source_nonblocking() -> dict | None:
    """저장된 웹 그리드 결과를 바로 돌려주고, 오래됐으면 뒤에서 갱신을 건다."""
    now = time.time()
    with _web_state_lock:
        if not _web_state["loaded"]:
            _load_web_snapshot_from_disk()
        valid = _web_state["value"] is not None and _web_state["key"] == _web_key()
        value = _copy_source(_web_state["value"]) if valid else None
        stale = not valid or now - _web_state["fetched_at"] >= _WEB_TTL_SEC
        # 실패가 반복될 때 요청마다 브라우저를 다시 띄우지 않도록 재시도 간격을 둔다.
        retry_due = now - _web_state["attempt_at"] >= _WEB_RETRY_SEC
    if stale and retry_due:
        _start_web_refresh()
    return value


def prewarm() -> None:
    """기동 직후 웹 그리드 수집을 백그라운드로 건다 (계정 미설정이면 생략)."""
    if _web_configured():
        _web_source_nonblocking()


def _cached_if(key: tuple, fresh_after: float) -> dict | None:
    with _cache_lock:
        cached = _cache.get("value")
        if cached is not None and _cache.get("key") == key and float(_cache.get("at") or 0.0) > fresh_after:
            return _copy_source(cached)
    return None


def get_patched_source(force_refresh: bool = False) -> dict:
    """화면용 패치 근거. 웹 그리드가 설정돼 있으면 저장된 수집 결과를 즉시 쓰고,
    아직 한 번도 수집되지 않았으면 수집이 끝날 때까지 REST/엑셀 값을 보여준다.
    force_refresh는 웹 그리드까지 동기로 다시 읽는다."""
    if _web_configured():
        web = _refresh_web_now() if force_refresh else _web_source_nonblocking()
        if web is not None:
            return web
    return _get_fallback_source(force_refresh)


def _get_fallback_source(force_refresh: bool) -> dict:
    key = _cache_key()
    requested_at = time.time()
    # force_refresh면 이 요청 이후에 채워진 값만, 아니면 TTL 안의 값을 쓴다.
    fresh_after = requested_at if force_refresh else requested_at - _CACHE_TTL_SEC
    if not force_refresh and (cached := _cached_if(key, fresh_after)) is not None:
        return cached

    with _refresh_lock:
        # 락을 기다리는 동안 다른 요청이 이미 갱신했으면 그 결과를 쓴다.
        if (cached := _cached_if(key, fresh_after)) is not None:
            return cached
        source = _read_fallback_source()
        with _cache_lock:
            _cache["key"] = key
            _cache["at"] = time.time()
            _cache["value"] = _copy_source(source)
    return _copy_source(source)


def patch_levels_for(items: list[dict], export: dict | None = None) -> dict[str, str]:
    ex = export if export is not None else get_patched_source()
    if not ex["hosts"] and not ex["ips"]:
        return {}

    levels: dict[str, str] = {}
    for item in items:
        host = (item.get("hostname") or "").strip().lower()
        if not host:
            continue
        if host in ex["hosts"]:
            levels[host] = ex["levels"].get(host, "")
            continue
        for ip in _IP_PATTERN.findall(item.get("ip") or ""):
            if ip in ex["ips"]:
                levels[host] = ex["levels"].get(ip, "")
                break
    return levels
