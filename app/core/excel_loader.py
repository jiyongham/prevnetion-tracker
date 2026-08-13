# app/core/excel_loader.py
from pathlib import Path
import pandas as pd
from app.config import settings

HALF_COLS = {
    "H1": {
        "schedule": "상반기 일정",
        "mode": "실 전환 / 무중단",
        "done": "상반기 완료",
    },
    "H2": {
        "schedule": "하반기 일정",
        "mode": "실 전환 / 무중단.1",
        "done": "하반기 완료",
    },
}


def _s(row, col: str) -> str:
    """안전하게 문자열 추출"""
    val = row.get(col, "")
    if pd.isna(val):
        return ""
    return str(val).strip()


def load_dr_items(excel_path: str | None = None, half: str = "H2") -> list[dict]:
    """DR 모의훈련 대상 엑셀 로드 (half: H1/H2)"""
    path = Path(excel_path or settings.excel_path)
    if not path.exists():
        raise FileNotFoundError(f"엑셀 파일 없음: {path}")

    if half not in HALF_COLS:
        raise ValueError(f"half는 H1 또는 H2여야 합니다: {half}")

    df = pd.read_excel(path, dtype=str)
    cols = HALF_COLS[half]

    items = []
    for _, row in df.iterrows():
        system = _s(row, "시스템명")
        no = _s(row, "NO")

        # 시스템명/NO 둘 다 없으면 빈 행으로 판단
        if not system and not no:
            continue

        items.append({
            "no": no,
            "company": _s(row, "관계사"),
            "business_name": _s(row, "주업무명"),
            "system_name": system,
            "hostname": _s(row, "호스트명"),
            "ip": _s(row, "IP"),
            "manage_part": _s(row, "관리파트"),
            "ops_team": _s(row, "APP운영팀"),
            "owner": _s(row, "담당자"),
            "is_target": _s(row, "대상 여부(O,X)").upper() == "O",
            "exclude_reason": _s(row, "기타 (제외 사유)"),
            "evidence": _s(row, "증적"),
            # 반기별
            "half": half,
            "schedule_raw": _s(row, cols["schedule"]),
            "mode": _s(row, cols["mode"]),
            "excel_done": _s(row, cols["done"]),
            "prevention_type": "예방3",
        })

    return items


def get_targets(items: list[dict]) -> list[dict]:
    """대상 여부 O만 (완료율 분모)"""
    return [i for i in items if i["is_target"]]
