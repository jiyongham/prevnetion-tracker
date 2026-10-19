# app/services/kernel_patched.py
"""
'패치 완료가 확인된 서버' 목록을 읽어 판정에 넘길 집합을 만든다.

경위: Polestar REST에는 OS 패치 레벨 필드가 없다(인터페이스 정의서 108개 엔드포인트
전수 확인). 화면의 PQL 검색은 `config[OS 패치 레벨]="4.18.0-553%"` 로 **거르는 것은
되지만 값을 돌려주지는 않는다**. 다행히 판정에 필요한 건 값이 아니라 "누가 패치됐나"라
PQL 결과를 엑셀로 내보내 그 목록만 받으면 충분하다.

그래서 이 모듈은 값 비교를 하지 않는다. 파일에 들어 있다 = 타겟 패턴에 걸렸다 = 완료.
나중에 REST가 열리면 read_patched_hosts()만 그쪽 호출로 바꾸면 판정부는 그대로다.

컬럼명은 Polestar 내보내기 형식에 맡기지 않고 별칭으로 찾는다 - 화면 설정이나 버전에
따라 머리글이 달라져도 파일을 다시 만들 필요가 없게.
"""
import logging
import re
from pathlib import Path

import pandas as pd

from app.config import settings

logger = logging.getLogger(__name__)

# 내보내기 머리글이 제각각일 수 있어 후보를 넓게 잡는다 (공백 제거·소문자 비교)
_HOST_HEADERS = {"호스트명", "hostname", "hostnme", "host", "서버명", "name", "리소스명", "리소스이름"}
_IP_HEADERS = {"ip", "ip주소", "ipaddress", "아이피"}
# OS 패치 레벨(= 현재 커널 버전) 값 컬럼. 이게 있으면 '목록에 있음'이 아니라
# '타겟 커널 이상인지'로 판정한다 (kernel.judge_kernel).
_LEVEL_HEADERS = {
    "os패치레벨", "ospatchlevel", "패치레벨", "patchlevel",
    "커널버전", "커널", "kernel", "kernelversion", "kernelrelease",
}

_IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _norm_header(h) -> str:
    return str(h or "").replace(" ", "").strip().lower()


def _pick_column(df: pd.DataFrame, headers: set[str]) -> str | None:
    for c in df.columns:
        if _norm_header(c) in headers:
            return c
    return None


def _empty() -> dict:
    return {"hosts": set(), "ips": set(), "levels": {}, "rows": 0, "source": "", "has_level": False}


def read_patched_export(path: str | None = None) -> dict:
    """
    PQL 결과 내보내기 파일 -> {"hosts": {호스트명(소문자)}, "ips": {IP}, "rows": 건수}

    파일이 없으면 빈 결과를 돌려준다 - 아직 안 올렸을 뿐이지 오류가 아니다.
    (그 상태에서는 수동 완료 체크만으로 판정된다.)
    """
    raw = path or settings.kernel_patched_export_path
    if not raw:
        return _empty()
    p = Path(raw)
    if not p.exists():
        return _empty()

    try:
        df = pd.read_excel(p, dtype=str).fillna("")
    except ImportError as e:
        # 레거시 .xls 는 xlrd 가 있어야 읽힌다. 없으면 조용히 0건이 되어
        # "완료가 왜 안 붙지" 로 헤매므로 무엇이 없는지 남긴다.
        logger.error(f"패치 확인 파일을 읽지 못했습니다 ({p.name}): {e} — .xlsx 로 저장하거나 xlrd 설치 필요")
        return _empty()
    host_col = _pick_column(df, _HOST_HEADERS)
    ip_col = _pick_column(df, _IP_HEADERS)
    level_col = _pick_column(df, _LEVEL_HEADERS)

    hosts: set[str] = set()
    ips: set[str] = set()
    # 호스트명/IP -> OS 패치 레벨. 값 컬럼이 없는 예전 형식이면 비어 있고,
    # 그때는 판정이 '목록에 있음 = 완료'로 떨어진다.
    levels: dict[str, str] = {}
    for _, row in df.iterrows():
        level = str(row[level_col]).strip() if level_col else ""
        if host_col:
            v = str(row[host_col]).strip().lower()
            if v:
                hosts.add(v)
                if level:
                    levels[v] = level
        if ip_col:
            # 한 셀에 IP가 여러 개 적히는 경우가 있어 패턴으로 뽑는다
            found = _IP_PATTERN.findall(str(row[ip_col]))
            ips.update(found)
            if level:
                levels.update({ip: level for ip in found})

    if not host_col and not ip_col:
        logger.warning(
            f"패치 확인 파일에서 호스트명/IP 컬럼을 못 찾았습니다 ({p.name}). "
            f"컬럼: {list(df.columns)[:10]}"
        )
    if not level_col:
        logger.info(
            f"패치 확인 파일에 OS 패치 레벨 컬럼이 없습니다 ({p.name}) - "
            f"목록에 있으면 완료로 봅니다. 컬럼: {list(df.columns)[:10]}"
        )

    return {
        "hosts": hosts, "ips": ips, "levels": levels,
        "rows": len(df), "source": p.name, "has_level": bool(level_col),
    }


def patch_levels_for(items: list[dict], export: dict | None = None) -> dict[str, str]:
    """
    우리 대상 목록 기준 {호스트명(소문자): OS 패치 레벨}.

    호스트명으로 먼저 맞추고, 없으면 IP로 보강한다. Polestar 리소스명이 호스트명이 아니라
    한글 서버명인 경우가 있어(EoS에서 확인된 패턴) IP가 실질적인 보조 키다.

    값 컬럼이 없는 예전 형식 파일이면 레벨이 빈 문자열이 된다 - 판정 쪽에서 그때는
    '목록에 있음 = 완료'로 떨어뜨린다(kernel.judge_kernel). 키가 아예 없는 것과
    값이 빈 것은 다르다: 전자는 패치 안 된 서버, 후자는 패치됐지만 값을 모르는 서버다.
    """
    ex = export if export is not None else read_patched_export()
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
