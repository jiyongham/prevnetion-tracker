# app/core/kernel_loader.py
"""
OS 커널 패치 대상 엑셀 로드.

다른 도메인과 달리 이 엑셀은 '자산 목록'만 온다 - 조치계획도 완료 컬럼도 없다.
그래서 계획은 전적으로 화면에서 취합하고(kernel_input), 완료는 외부 근거로 판정한다.

개발기와 운영기가 별도 파일로 오므로 scope('dev'/'prod')로 파일을 가른다.
운영기 파일이 아직 없으면 그 범위는 빈 목록이 되고 화면에서도 조용히 빠진다.
"""
from pathlib import Path

import pandas as pd

from app.config import settings
from app.models.db import get_kernel_inputs

SHEET_NAME = "대상 서버(개발)"

SCOPE_LABELS = {"dev": "개발기", "prod": "운영기"}


def _s(row, col: str) -> str:
    """안전하게 문자열 추출"""
    val = row.get(col, "")
    if pd.isna(val):
        return ""
    return str(val).strip()


def excel_path_for(scope: str) -> str:
    return settings.kernel_dev_excel_path if scope == "dev" else settings.kernel_prod_excel_path


def available_scopes() -> list[str]:
    """파일이 실제로 준비된 범위만. 운영기 확대 전에는 ['dev'] 하나다."""
    return [s for s in ("dev", "prod") if (p := excel_path_for(s)) and Path(p).exists()]


# 엑셀 파싱은 호출당 1초 남짓인데 한 화면에서 여러 번 읽힌다.
# 파일 수정시각이 바뀌면 자동으로 다시 읽으므로 엑셀을 교체해도 재시작할 필요가 없다.
_items_cache: dict = {}   # (경로, mtime, scope) -> items


def load_kernel_items(scope: str = "dev", excel_path: str | None = None) -> list[dict]:
    """대상 서버 엑셀 로드 (파일 mtime 기준 캐시)"""
    raw_path = excel_path or excel_path_for(scope)
    if not raw_path:
        return []
    path = Path(raw_path)
    if not path.exists():
        return []

    cache_key = (str(path), path.stat().st_mtime_ns, scope)
    cached = _items_cache.get(cache_key)
    if cached is not None:
        # 호출부가 항목을 수정(DB 병합)하므로 캐시 원본이 오염되지 않게 사본을 준다
        return [dict(i) for i in cached]

    # 시트명이 범위마다 다를 수 있어(운영기 파일은 아직 미확인) 첫 시트를 기본으로 삼되,
    # 알고 있는 이름이 있으면 그걸 우선한다.
    sheets = pd.ExcelFile(path).sheet_names
    sheet = SHEET_NAME if SHEET_NAME in sheets else sheets[0]
    df = pd.read_excel(path, sheet_name=sheet, dtype=str)

    items = []
    for _, row in df.iterrows():
        insight_key = _s(row, "Key")
        name = _s(row, "서버명")
        if not insight_key and not name:
            continue

        items.append({
            "item_no": insight_key or name,
            "no": insight_key or name,      # match_items_by_ip 가 item["no"] 로 색인한다
            "insight_key": insight_key,
            "scope": scope,
            "system_name": name,
            "hostname": _s(row, "호스트명"),
            "ip": _s(row, "IP"),
            "vm_type": _s(row, "가상/일반 구분"),
            "status_raw": _s(row, "상태"),
            "center": _s(row, "센터구분"),
            "company": _s(row, "자산구분"),
            "ops_team": _s(row, "시스템운영팀"),
            "owner": _s(row, "시스템담당자"),
            "server_part": _s(row, "서버관리파트"),
            "os": _s(row, "OS"),            # 엑셀 시점의 OS = 패치 전 기준값
            "db": _s(row, "DB"),
            "infra_type": _s(row, "통합인프라 종류"),
            # 이 엑셀엔 계획/완료 컬럼이 없다. 값은 전부 DB 병합 단계에서 채워진다.
            "schedule_raw": "",
            "excel_done": "",
        })

    # 엑셀이 교체되면(mtime 변경) 예전 키는 쓸모없으니 지운다. 범위는 dev/prod 둘 다 남긴다.
    for stale in [k for k in _items_cache if k[0] == str(path) and k[1] != cache_key[1]]:
        del _items_cache[stale]
    _items_cache[cache_key] = items
    return [dict(i) for i in items]


def load_kernel_items_merged(scope: str = "dev", excel_path: str | None = None) -> list[dict]:
    """엑셀 + 웹 입력값 병합 (웹 값이 우선). 계획·완료·제외는 전부 웹에서 온다."""
    items = load_kernel_items(scope=scope, excel_path=excel_path)
    inputs = get_kernel_inputs()

    for item in items:
        db = inputs.get(item["item_no"])
        item["input_source"] = "excel"
        item["is_excluded"] = False
        item["exclude_reason"] = ""
        item["evidence"] = ""
        item["note"] = ""
        item["updated_by"] = ""
        item["updated_at"] = ""

        if not db:
            continue

        if db.get("schedule"):
            item["schedule_raw"] = db["schedule"]
            item["input_source"] = "web"
        if db.get("is_done"):
            item["excel_done"] = "O"
            item["input_source"] = "web"
        if db.get("owner"):
            item["owner"] = db["owner"]
            item["input_source"] = "web"
        item["is_excluded"] = bool(db.get("is_excluded"))
        item["exclude_reason"] = db.get("exclude_reason") or ""
        item["evidence"] = db.get("evidence") or ""
        item["note"] = db.get("note") or ""
        item["updated_by"] = db.get("updated_by") or ""
        item["updated_at"] = db.get("updated_at") or ""

    return items


def get_targets(items: list[dict]) -> list[dict]:
    """완료율 분모. 관리자가 제외한 대상만 뺀다 (엑셀엔 제외 개념이 없다)."""
    return [i for i in items if not i.get("is_excluded")]
