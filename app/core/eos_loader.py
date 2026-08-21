# app/core/eos_loader.py
import calendar
import re
from datetime import date
from pathlib import Path

import pandas as pd
from app.config import settings
from app.models.db import get_eos_inputs
from app.services.eos_products import load_eos_product_table, match_db_eos_date, match_os_eos_date

# "EOS 진행 (프로젝트) → 제외 (내년)"처럼 화살표로 상태가 갱신된 경우, 화살표 뒤(최신) 값이 진짜 현재 상태
_MONTH_YEAR_PATTERN = re.compile(r"(?:(\d{2,4})년\s*)?(\d{1,2})월")


def _s(row, col: str) -> str:
    """안전하게 문자열 추출"""
    val = row.get(col, "")
    if pd.isna(val):
        return ""
    return str(val).strip()


def _effective(raw: str) -> str:
    """'A → B' 형태면 마지막(최신) 값만 사용"""
    raw = (raw or "").strip()
    if "→" in raw:
        raw = raw.split("→")[-1].strip()
    return raw


def classify_eos_status(raw: str) -> str:
    """
    'EOS 진행/폐기 예정/제외' 컬럼 원문 -> target/excluded/no_reply/other 4분류.
    자유 텍스트라 표현이 제각각이라(예: 'EOS 진행 (프로젝트)', '제외 (폐기 예정)',
    'EOS 진행 (프로젝트) → 폐기예정', '미응답 → 내년 상반기') 접두어 + 키워드로 판단.
    """
    eff = _effective(raw)
    if eff.lower().startswith("eos 진행"):
        return "target"
    # '폐기'(이미 폐기/폐기예정)나 '내년'(다음 반기로 연기)이면 이번 반기 EoS 전환 대상은 아님 -> 제외로 취급
    if eff.startswith("제외") or "폐기" in eff or "내년" in eff:
        return "excluded"
    if eff.startswith("미응답"):
        return "no_reply"
    return "other"


def parse_eos_schedule(raw: str, base_year: int) -> date | None:
    """
    '조치계획 (OO월)' 컬럼 파싱. 일(day) 정보가 없어 월의 마지막 날로 잡는다
    (예: '8월' -> 그 달 말일. 그래야 그 달이 다 지나야 '기한 경과'로 본다).
    '10월 → 8월'처럼 화살표가 있으면 마지막(최신) 값만, '27년 5월'처럼 연도가 있으면 그 연도로.
    """
    eff = _effective(raw)
    m = _MONTH_YEAR_PATTERN.search(eff)
    if not m:
        return None

    year_part, month_part = m.groups()
    month = int(month_part)
    if not (1 <= month <= 12):
        return None

    year = base_year
    if year_part:
        y = int(year_part)
        year = y if y > 100 else 2000 + y  # '27' -> 2027

    last_day = calendar.monthrange(year, month)[1]
    try:
        return date(year, month, last_day)
    except ValueError:
        return None


def load_eos_items(excel_path: str | None = None) -> list[dict]:
    """EoS 대상 엑셀 로드"""
    path = Path(excel_path or settings.eos_excel_path)
    if not path.exists():
        raise FileNotFoundError(f"EoS 엑셀 없음: {path}")

    df = pd.read_excel(path, sheet_name="EOS대상(OS,DB)", dtype=str)
    product_table = load_eos_product_table(excel_path)
    today = date.today()

    items = []
    for _, row in df.iterrows():
        insight_key = _s(row, "Key")
        system_name = _s(row, "Label")
        if not insight_key and not system_name:
            continue

        status_raw = _s(row, "EOS 진행/폐기 예정/제외")
        is_target = classify_eos_status(status_raw) == "target"
        os_val = _s(row, "OS")
        db_val = _s(row, "DB")
        os_eos_date = match_os_eos_date(os_val, product_table)
        db_eos_date = match_db_eos_date(db_val, product_table)

        items.append({
            "item_no": insight_key or system_name,  # Insight Key가 없으면 시스템명으로 대체
            "no": insight_key or system_name,       # match_items_by_ip가 item["no"]로 색인함 (item_no와 동일값)
            "insight_key": insight_key,
            "object_type": _s(row, "Object Type"),
            "status_raw": status_raw,
            "status": classify_eos_status(status_raw),
            "is_target": is_target,
            "exclude_reason": _s(row, "기타 (제외사유)"),
            "company": _s(row, "자산구분"),
            "system_name": system_name,
            "hostname": _s(row, "호스트명"),
            "ip": _s(row, "IP"),
            "cmdb_status": _s(row, "상태"),
            "virt_type": _s(row, "가상/일반 구분"),
            "center": _s(row, "센터구분"),
            "os": os_val,
            "db": db_val,
            # '제품별 EoS 일정' 표 기준 공식 EOS일자. 이미 지났고(오늘 이후 아님) 대상(target)이면
            # 그 항목은 OS/DB 트랙 각각의 EoS 대상으로 집계한다 (리포트의 [OS]/[DB] 구분 기준).
            "os_eos_date": os_eos_date,
            "db_eos_date": db_eos_date,
            "os_eos_target": is_target and bool(os_eos_date) and os_eos_date <= today,
            "db_eos_target": is_target and bool(db_eos_date) and db_eos_date <= today,
            "infra_type": _s(row, "통합인프라 종류"),
            "hw": _s(row, "HW장비"),
            "manage_part": _s(row, "서버관리부서"),
            "server_part": _s(row, "서버파트"),
            "ops_team": _s(row, "시스템운영팀"),
            "owner": _s(row, "시스템담당자"),
            "schedule_raw": _s(row, "조치계획 (OO월)"),
            "excel_done": "",  # 엑셀엔 완료 컬럼이 없음 - 완료는 순전히 JIRA 또는 관리자 수동체크로만 판단
        })
    return items


def get_targets(items: list[dict]) -> list[dict]:
    """EOS 진행(target) 상태인 것만 (완료율 분모)"""
    return [i for i in items if i["is_target"]]


def load_eos_items_merged(excel_path: str | None = None) -> list[dict]:
    """엑셀 + DB 입력값 병합 (DB 값이 우선)"""
    items = load_eos_items(excel_path=excel_path)
    inputs = get_eos_inputs()

    for item in items:
        db = inputs.get(item["item_no"])
        item["input_source"] = "excel"
        item["evidence"] = ""

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

            item["note"] = db.get("note", "")
            item["updated_by"] = db.get("updated_by", "")
            item["updated_at"] = db.get("updated_at", "")
        else:
            item["note"] = ""
            item["updated_by"] = ""
            item["updated_at"] = ""

    return items
