# app/core/capacity_loader.py
from pathlib import Path
import pandas as pd
from app.config import settings
from app.models.db import get_capacity_inputs

# 시트별 용량 컬럼명이 달라서 매핑 (DATA=일반 ASM/파일시스템, ARCH=아카이브)
SHEET_CAPACITY_COLS = {
    "DATA": {"total": "전체 용량(GB)", "remaining": "남은 용량(GB)", "usage_pct": "사용량(%)"},
    "ARCH": {"total": "아카이브 전체 용량(GB)", "remaining": None, "usage_pct": None},
}


def _s(row, col: str) -> str:
    """안전하게 문자열 추출"""
    val = row.get(col, "")
    if pd.isna(val):
        return ""
    return str(val).strip()


def _f(row, col: str | None) -> float | None:
    """안전하게 숫자 추출"""
    if not col:
        return None
    val = row.get(col)
    if val is None or pd.isna(val):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def load_capacity_items(sheet: str, excel_path: str | None = None) -> list[dict]:
    """용량관리 엑셀 로드 (sheet: DATA/ARCH)"""
    path = Path(excel_path or settings.capacity_excel_path)
    if not path.exists():
        raise FileNotFoundError(f"용량관리 엑셀 없음: {path}")
    if sheet not in SHEET_CAPACITY_COLS:
        raise ValueError(f"sheet는 DATA 또는 ARCH여야 합니다: {sheet}")

    df = pd.read_excel(path, sheet_name=sheet, dtype=str)
    cap_cols = SHEET_CAPACITY_COLS[sheet]

    items = []
    for _, row in df.iterrows():
        no = _s(row, "NO")
        ci_name = _s(row, "CI명")
        if not no and not ci_name:
            continue

        items.append({
            "item_no": f"{sheet}:{no}",
            "sheet": sheet,
            "no": no,
            "ci_name": ci_name,
            "hostname": _s(row, "HOSTNAME"),
            "ip": _s(row, "IP"),
            "company": _s(row, "자산 구분"),
            "fs_type": _s(row, "파일시스템 종류"),
            "infra_type": _s(row, "통합인프라 종류"),
            "cluster_type": _s(row, "클러스터 종류"),
            "center": _s(row, "센터 구분"),
            "total_gb": _f(row, cap_cols["total"]),
            "remaining_gb": _f(row, cap_cols["remaining"]),
            "usage_pct": _f(row, cap_cols["usage_pct"]),
            "required_gb": _f(row, "증설 필요 용량(GB)"),
            "manage_part": _s(row, "서버 관리 파트"),
            "ops_team": _s(row, "시스템 운영팀"),
            "owner": _s(row, "시스템 담당자"),
            "expand_flag": _s(row, "증설 여부 (O,X)").upper(),  # "O"/"X"/"" (공란=미회신)
            "is_target": _s(row, "증설 여부 (O,X)").upper() == "O",
            "exclude_reason": _s(row, "기타(증설불가사유)"),
            "schedule_raw": _s(row, "증설 일정 (OO월OO일)"),
            "excel_done": _s(row, "증설 완료"),
            "done_date_raw": _s(row, "증설 일자"),
        })
    return items


def get_targets(items: list[dict]) -> list[dict]:
    """증설 여부 O만 (완료율 분모)"""
    return [i for i in items if i["is_target"]]


def load_capacity_items_merged(sheet: str, excel_path: str | None = None) -> list[dict]:
    """
    엑셀 + DB 입력값 병합 (DB 값이 우선). 병합 후 최종 상태(status_kind)를 확정한다:
    - excluded : 엑셀 "증설 여부"가 X, 또는 관리자가 웹에서 제외 버튼 처리
    - target   : 엑셀 "증설 여부"가 O, 또는 미회신이었지만 일정이 입력됨(증설 의사로 간주)
    - no_reply : 그 외 (증설 여부 공란 + 일정도 없음) - 진짜 아직 응답 없는 대상
    is_target는 이 status_kind == "target"과 동일 (완료율 분모로 쓰임).
    """
    items = load_capacity_items(sheet=sheet, excel_path=excel_path)
    inputs = get_capacity_inputs(sheet)

    for item in items:
        db = inputs.get(item["item_no"])
        item["input_source"] = "excel"
        item["evidence"] = ""
        is_excluded_web = False

        if db:
            if db.get("schedule"):
                item["schedule_raw"] = db["schedule"]
                item["input_source"] = "web"
            if db.get("is_done"):
                item["excel_done"] = "O"
                item["input_source"] = "web"
            if db.get("evidence"):
                item["evidence"] = db["evidence"]
            if db.get("owner"):
                item["owner"] = db["owner"]
                item["input_source"] = "web"
            is_excluded_web = bool(db.get("is_excluded"))

            item["note"] = db.get("note", "")
            item["updated_by"] = db.get("updated_by", "")
            item["updated_at"] = db.get("updated_at", "")
        else:
            item["note"] = ""
            item["updated_by"] = ""
            item["updated_at"] = ""

        item["is_excluded"] = item["expand_flag"] == "X" or is_excluded_web
        if item["is_excluded"]:
            item["is_target"] = False
            item["status_kind"] = "excluded"
        elif item["expand_flag"] == "O" or (item.get("schedule_raw") or "").strip():
            item["is_target"] = True
            item["status_kind"] = "target"
        else:
            item["is_target"] = False
            item["status_kind"] = "no_reply"

    return items
