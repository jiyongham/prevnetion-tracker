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


def norm_mode(value: str) -> str:
    """수행방식 표기 정규화. 엑셀/웹에 '실 전환'과 '실전환'이 섞여 있어 공백을 없앤다."""
    return (value or "").replace(" ", "").strip()


# 엑셀 파싱은 호출당 약 0.7초인데, 대시보드 한 번에 여러 번 읽힌다
# (하반기 항목 + 상반기 무중단 대상 확인 + 화면별 재조회). 파일 수정시각이 바뀌면
# 자동으로 다시 읽으므로 엑셀을 교체해도 재시작할 필요는 없다.
_items_cache: dict = {}   # (경로, mtime, half) -> items


def load_dr_items(excel_path: str | None = None, half: str = "H2") -> list[dict]:
    """DR 모의훈련 대상 엑셀 로드 (half: H1/H2, 파일 mtime 기준 캐시)"""
    path = Path(excel_path or settings.excel_path)
    if not path.exists():
        raise FileNotFoundError(f"엑셀 파일 없음: {path}")

    if half not in HALF_COLS:
        raise ValueError(f"half는 H1 또는 H2여야 합니다: {half}")

    cache_key = (str(path), path.stat().st_mtime_ns, half)
    cached = _items_cache.get(cache_key)
    if cached is not None:
        # 호출부가 항목을 수정(DB 병합 등)하므로 캐시 원본이 오염되지 않게 사본을 준다
        return [dict(i) for i in cached]

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
            # 증적: H1은 엑셀 공유 컬럼값을 그대로 쓰지만(문서화된 기존 동작 유지),
            # H2는 엑셀 값을 절대 쓰지 않는다. '증적' 컬럼이 반기별로 나뉘어 있지 않아서,
            # 상반기에 이미 종결된 JIRA 티켓을 증적으로 적어둔 행이 그대로 하반기(무중단
            # 대상)에도 노출돼 - 하반기엔 아무도 새로 증적을 입력한 적이 없는데도 상반기
            # 증적이 인정된 것처럼 보이는 문제가 있었다. H2는 아래 load_dr_items_merged()에서
            # DB(schedule_input, half='H2')에 실제로 입력된 값만 채워 넣는다 - 그 전까지는
            # 빈 값(증적 없음)으로 둬야 '하반기엔 새로 증적을 받는다'는 요구사항이 지켜진다.
            "evidence": _s(row, "증적") if half == "H1" else "",
            # 반기별
            "half": half,
            "schedule_raw": _s(row, cols["schedule"]),
            "mode": norm_mode(_s(row, cols["mode"])),
            "excel_done": _s(row, cols["done"]),
            "prevention_type": "예방3",
        })

    # 엑셀이 교체되면(mtime 변경) 예전 키는 쓸모없으니 지운다. 반기는 H1/H2 둘 다 남긴다 -
    # 하반기 화면이 분모를 구할 때 상반기 무중단 대상을 같이 읽기 때문에 서로 밀어내면 안 된다.
    for stale in [k for k in _items_cache if k[:2] != cache_key[:2]]:
        del _items_cache[stale]
    _items_cache[cache_key] = items
    return [dict(i) for i in items]


def get_targets(items: list[dict]) -> list[dict]:
    """대상 여부 O만 (완료율 분모)"""
    return [i for i in items if i["is_target"]]


def get_h1_nonstop_target_nos(excel_path: str | None = None) -> set[str]:
    """
    상반기 무중단으로 수행한 대상 NO 집합.
    하반기 DR 모의훈련은 이 대상에 한해 수행하므로 완료율 분모로 사용.
    """
    h1 = load_dr_items(excel_path=excel_path, half="H1")
    return {
        i["no"] for i in h1
        if i["is_target"] and "무중단" in (i.get("mode") or "")
    }


def scope_h2_targets(items: list[dict], excel_path: str | None = None) -> list[dict]:
    """하반기(H2) 항목을 상반기 무중단 대상으로만 한정"""
    nonstop_nos = get_h1_nonstop_target_nos(excel_path=excel_path)
    return [i for i in items if i["no"] in nonstop_nos]


def get_h1_real_target_nos(excel_path: str | None = None) -> set[str]:
    """
    상반기 '실전환'으로 수행한 대상 NO 집합.
    하반기 DR 모의훈련 통계(완료율/리포트)는 상반기 무중단 대상(get_h1_nonstop_target_nos)
    으로만 한정되고 이 실전환 대상은 거기 안 들어간다 - 다만 담당자들이 하반기에도 이
    대상들의 일정/증적을 참고삼아 같이 입력하고 싶어해서, 화면에는 별도 참고 목록으로
    노출한다 (app.services.dr_data.load_extra_h2_items 참고). 통계에는 절대 포함하지 않는다.
    """
    h1 = load_dr_items(excel_path=excel_path, half="H1")
    return {
        i["no"] for i in h1
        if i["is_target"] and "실전환" in (i.get("mode") or "")
    }

# app/core/excel_loader.py 맨 아래 추가
from app.models.db import get_inputs


def load_dr_items_merged(half: str = "H2", excel_path: str | None = None) -> list[dict]:
    """엑셀 + DB 입력값 병합 (DB 값이 우선)"""
    items = load_dr_items(excel_path=excel_path, half=half)
    inputs = get_inputs(half)

    for item in items:
        db = inputs.get(item["no"])
        item["input_source"] = "excel"

        if db:
            # DB에 값이 있으면 덮어쓰기
            if db.get("schedule"):
                item["schedule_raw"] = db["schedule"]
                item["input_source"] = "web"
            if db.get("mode"):
                item["mode"] = norm_mode(db["mode"])
            if db.get("is_done"):
                item["excel_done"] = "O"
                item["input_source"] = "web"
            if db.get("evidence"):
                item["evidence"] = db["evidence"]
            if db.get("owner"):
                item["owner"] = db["owner"]
                item["input_source"] = "web"

            # 웹에서 제외 처리하며 남긴 사유. 엑셀 원본의 '기타 (제외 사유)'와는 별개라
            # 덮어쓰지 않고 별도 필드로 둔다 (원본은 반기 계획 수립 시의 사유).
            item["web_exclude_reason"] = db.get("exclude_reason") or ""
            item["note"] = db.get("note", "")
            item["updated_by"] = db.get("updated_by", "")
            item["updated_at"] = db.get("updated_at", "")
        else:
            item["web_exclude_reason"] = ""
            item["note"] = ""
            item["updated_by"] = ""
            item["updated_at"] = ""

    return items
