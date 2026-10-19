# app/core/kernel_loader.py
"""
OS 커널 패치 대상 엑셀 로드.

차수별 패치 계획서('커널 패치_N차.xlsx') 형식을 읽는다. 예전 자산목록 형식과 달리
작업일자/완료여부가 엑셀 안에 들어 있어서, 계획과 수동 완료표기가 파일에서 바로 온다
(웹 입력은 그 위에 덮어쓴다 - load_kernel_items_merged).

컬럼은 머리글 이름으로 찾되 별칭을 넓게 둔다. 차수마다 사람이 새로 만드는 파일이라
'담당자'/'시스템담당자'처럼 표기가 흔들리는데, 그때마다 코드를 고치면 차수를 못 넘긴다.

개발기와 운영기가 별도 파일로 오므로 scope('dev'/'prod')로 파일을 가른다.
운영기 파일이 아직 없으면 그 범위는 빈 목록이 되고 화면에서도 조용히 빠진다.
"""
from pathlib import Path

import pandas as pd

from app.config import settings
from app.models.db import get_kernel_inputs

SHEET_NAME = "대상 서버(개발)"

# 머리글 별칭 (공백 제거·소문자 비교). 앞쪽이 현재 차수 파일의 표기.
_COLUMNS = {
    "system_name": {"서버명", "시스템명", "name"},
    "ip": {"ip", "ip주소", "아이피"},
    "hostname": {"호스트명", "hostname", "host"},
    "os": {"os", "운영체제"},
    "ops_team": {"시스템운영팀", "운영팀", "app운영팀"},
    "owner": {"담당자", "시스템담당자", "운영담당자"},
    "server_part": {"서버관리파트", "관리파트", "서버파트"},
    "schedule_raw": {"작업일자", "작업일", "일정", "조치계획"},
    "work_time": {"시간", "작업시간"},
    "excel_done": {"완료여부", "완료", "진행상태"},
    "safer": {"safer삭제/설치", "safer삭제설치", "safer"},
}

SCOPE_LABELS = {"dev": "개발기", "prod": "운영기"}


def _s(row, col: str | None) -> str:
    """안전하게 문자열 추출 (col이 None이면 그 컬럼이 파일에 없다는 뜻)"""
    if not col:
        return ""
    val = row.get(col, "")
    if pd.isna(val):
        return ""
    text = str(val).strip()
    # pandas가 날짜/시간으로 읽으면 '2026-10-06 00:00:00'처럼 꼬리가 붙는다.
    # 화면과 일정 파싱 모두 날짜만 쓰므로 여기서 잘라둔다.
    return text[:10] if text.endswith("00:00:00") else text


def _norm_header(h) -> str:
    return str(h or "").replace(" ", "").strip().lower()


def _resolve_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """{필드: 실제 컬럼명}. 못 찾은 필드는 None (그 값은 빈 문자열이 된다)."""
    by_norm = {_norm_header(c): c for c in df.columns}
    return {
        field: next((by_norm[a] for a in aliases if a in by_norm), None)
        for field, aliases in _COLUMNS.items()
    }


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

    cols = _resolve_columns(df)

    items = []
    for _, row in df.iterrows():
        name = _s(row, cols["system_name"])
        hostname = _s(row, cols["hostname"])
        if not name and not hostname:
            continue

        # 이 파일엔 Insight Key가 없다. 호스트명이 가장 안정적인 키라 그걸 쓰고,
        # 없으면 서버명으로 떨어진다 (웹 입력 DB가 이 값으로 묶이므로 차수가 바뀌어도
        # 같은 서버면 같은 키가 나와야 한다).
        key = hostname or name

        items.append({
            "item_no": key,
            "no": key,                      # match_items_by_ip 가 item["no"] 로 색인한다
            "insight_key": "",
            "scope": scope,
            "system_name": name,
            "hostname": hostname,
            "ip": _s(row, cols["ip"]),
            "vm_type": "",
            "status_raw": "",
            "center": "",
            "company": "",
            "ops_team": _s(row, cols["ops_team"]),
            "owner": _s(row, cols["owner"]),
            "server_part": _s(row, cols["server_part"]),
            "os": _s(row, cols["os"]),      # 엑셀 시점의 OS = 패치 전 기준값
            "db": "",
            "infra_type": "",
            "work_time": _s(row, cols["work_time"]),
            "safer": _s(row, cols["safer"]),
            # 차수 계획서에는 작업일자/완료여부가 들어 있다 (예전 자산목록 형식에는 없었다).
            # 웹에서 입력한 값이 있으면 병합 단계에서 이 위에 덮어쓴다.
            "schedule_raw": _s(row, cols["schedule_raw"]),
            "excel_done": _s(row, cols["excel_done"]),
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
        item["last_inputter"] = ""
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
        item["last_inputter"] = db.get("last_inputter") or ""
        item["updated_at"] = db.get("updated_at") or ""

    return items


def get_targets(items: list[dict]) -> list[dict]:
    """완료율 분모. 관리자가 제외한 대상만 뺀다 (엑셀엔 제외 개념이 없다)."""
    return [i for i in items if not i.get("is_excluded")]
