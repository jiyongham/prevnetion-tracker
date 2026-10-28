# app/services/kernel_patched.py
"""커널 패치 완료 근거 수집 (웹 그리드 우선, REST/엑셀 폴백)."""
import logging
import hashlib
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

_CACHE_TTL_SEC = 300
_cache: dict = {"key": None, "at": 0.0, "value": None}
_cache_lock = threading.Lock()


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
    web_account_fingerprint = hashlib.sha256(
        f"{settings.kernel_polestar_web_user}\0{settings.kernel_polestar_web_password}".encode("utf-8")
    ).hexdigest()
    return (
        settings.kernel_polestar_view,
        web_account_fingerprint,
        settings.kernel_polestar_web_login_path,
        settings.kernel_polestar_web_grid_path,
        settings.kernel_polestar_browser_executable,
        raw_path,
        mtime,
        polestar.base_url,
    )


def invalidate_patched_source_cache() -> None:
    with _cache_lock:
        _cache["key"] = None
        _cache["at"] = 0.0
        _cache["value"] = None


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


def _read_preferred_source() -> dict:
    if settings.kernel_polestar_web_user and settings.kernel_polestar_web_password:
        try:
            from app.services.kernel_polestar_web import read_patched_web_grid

            web = read_patched_web_grid()
        except Exception as e:
            logger.warning("Polestar 웹 그리드 조회 실패 - 다음 패치 소스로 폴백합니다: %s", type(e).__name__)
        else:
            if web["rows"]:
                return web
            logger.warning("Polestar 웹 그리드에 유효한 패치 레벨 행이 없어 다음 소스로 폴백합니다")

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


def get_patched_source(force_refresh: bool = False) -> dict:
    key = _cache_key()
    now = time.time()
    with _cache_lock:
        cached = _cache.get("value")
        if (
            not force_refresh
            and cached is not None
            and _cache.get("key") == key
            and now - float(_cache.get("at") or 0.0) < _CACHE_TTL_SEC
        ):
            return _copy_source(cached)

    source = _read_preferred_source()
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
