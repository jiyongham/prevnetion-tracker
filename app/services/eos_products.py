# app/services/eos_products.py
"""
'제품별 EoS 일정' 시트(OS/DBMS 제품별 공식 EOS 일자표)를 읽어서,
메인 시트의 OS/DB 원문 문자열(예: 'Linux_Redhat7.9', 'MSSQL_SQLServer2016Ent_13.0.6300.2(SP3)')이
어느 제품 버전에 해당하는지 찾아 그 제품의 EOS 일자를 반환한다.

이 EOS 일자가 이미 지났으면(오늘 >= EOS일자) 그 항목은 OS 또는 DB EoS 대상으로 판단한다.
"""
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from app.config import settings


def _parse_version_tuple(text: str) -> tuple[int, ...]:
    """'7.0.7' -> (7,0,7), '8' -> (8,), '8.10' -> (8,10)"""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums) if nums else ()


def load_eos_product_table(excel_path: str | None = None) -> list[dict]:
    """'제품별 EoS 일정' 시트 -> [{kind, vendor, model, submodel, eos_date}, ...]"""
    path = Path(excel_path or settings.eos_excel_path)
    if not path.exists():
        return []

    df = pd.read_excel(path, sheet_name="제품별 EoS 일정", header=None, dtype=object)

    rows = []
    cur_kind = cur_vendor = cur_model = None
    for _, r in df.iterrows():
        kind, vendor, model, submodel, eos = r.get(1), r.get(2), r.get(3), r.get(4), r.get(5)
        if isinstance(kind, str) and kind.strip():
            cur_kind = kind.strip()
        if isinstance(vendor, str) and vendor.strip():
            cur_vendor = vendor.strip()
        # 모델명(예: 'SQL Server 2016')은 SP별 세부 행에서 병합돼있어 첫 행에만 채워짐 -> 이어받기
        if not pd.isna(model) and str(model).strip():
            cur_model = str(model).strip()
        if pd.isna(model) and pd.isna(submodel) and pd.isna(eos):
            continue
        if cur_kind not in ("OS", "DBMS"):
            continue

        eos_date = None
        if isinstance(eos, (datetime, date)):
            eos_date = eos.date() if isinstance(eos, datetime) else eos
        # "To be determined" 등 날짜가 아닌 값은 EOS 미정 -> None

        rows.append({
            "kind": cur_kind,
            "vendor": cur_vendor or "",
            "model": cur_model or "",
            "submodel": str(submodel).strip() if not pd.isna(submodel) else "",
            "eos_date": eos_date,
        })
    return rows


def _best_by_version(rows: list[dict], raw_version: tuple[int, ...]) -> dict | None:
    """숫자버전표(예: PostgreSQL 13~17, Redis 7/7.2/7.4)에서 raw_version이 속하는 줄 찾기.
    (해당 제품 라인 중 raw_version 이하인 것 중 가장 큰 버전 = 지금 그 버전이 속한 배포열)"""
    candidates = [(r, _parse_version_tuple(r["model"])) for r in rows]
    candidates = [(r, v) for r, v in candidates if v and v <= raw_version]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


def match_os_eos_date(raw: str, table: list[dict]) -> date | None:
    """OS 컬럼 원문(예: 'Linux_Redhat7.9', 'Windows_Window Server 2016') -> EOS 일자"""
    text = (raw or "").strip()
    if not text:
        return None
    os_rows = [r for r in table if r["kind"] == "OS"]

    m = re.search(r"redhat\s*linux\s*(\d+)|redhat\s*(\d+)", text, re.I)
    if m:
        major = m.group(1) or m.group(2)
        hit = next((r for r in os_rows if r["model"].lower() == f"redhat linux {major}".lower()), None)
        return hit["eos_date"] if hit else None

    m = re.search(r"oracle\s*(?:linux\s*)?(\d+)", text, re.I)
    if m:
        major = m.group(1)
        hit = next((r for r in os_rows if r["model"].lower() == f"oracle linux {major}".lower()), None)
        return hit["eos_date"] if hit else None

    m = re.search(r"window\s*server\s*(\d{4})\s*(r2)?", text, re.I)
    if m:
        model = f"Window Server {m.group(1)}" + (" R2" if m.group(2) else "")
        hit = next((r for r in os_rows if r["model"].lower() == model.lower()), None)
        return hit["eos_date"] if hit else None

    m = re.search(r"rocky\s*(?:linux)?\s*([\d.]+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in os_rows if r["vendor"] == "Linux(Rocky)"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    m = re.search(r"aix\s*([\d.]+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in os_rows if r["vendor"] == "AIX"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    m = re.search(r"solaris\s*([\d.]+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in os_rows if r["vendor"] == "Solaris"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    m = re.search(r"ubuntu\s*([\d.]+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in os_rows if r["vendor"] == "Ubuntu"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    return None


def match_db_eos_date(raw: str, table: list[dict]) -> date | None:
    """DB 컬럼 원문(예: 'Oracle_19cEnt_19.16.0.0.0', 'MSSQL_SQLServer2016Ent_13.0.6300.2(SP3)') -> EOS 일자"""
    text = (raw or "").strip()
    if not text:
        return None
    db_rows = [r for r in table if r["kind"] == "DBMS"]

    m = re.search(r"oracle[_\s]*(\d+)([cg])", text, re.I)
    if m:
        model = f"{m.group(1)}{m.group(2).lower()}"
        hit = next((r for r in db_rows if r["vendor"] == "Oracle" and r["model"].lower() == model.lower()), None)
        return hit["eos_date"] if hit else None

    m = re.search(r"sqlserver\s*(\d{4})", text, re.I)
    if m:
        model = f"SQL Server {m.group(1)}"
        rows_for_model = [r for r in db_rows if r["vendor"] == "MSSQL" and r["model"].lower() == model.lower()]
        sp_m = re.search(r"\(sp(\d+)", text, re.I)
        if sp_m:
            sp = f"sp{sp_m.group(1)}"
            hit = next((r for r in rows_for_model if sp in r["submodel"].lower()), None)
            if hit:
                return hit["eos_date"]
        # SP 표기가 없거나 매칭 실패 시 그 버전의 첫 행(RTM 등)으로
        return rows_for_model[0]["eos_date"] if rows_for_model else None

    m = re.search(r"tibero\s*(\d+)", text, re.I)
    if m:
        model = f"TIbero{m.group(1)}"
        hit = next((r for r in db_rows if r["vendor"] == "Tibero" and r["model"].lower() == model.lower()), None)
        return hit["eos_date"] if hit else None

    m = re.search(r"postgre(?:sql)?[_\s]*(\d+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in db_rows if r["vendor"] == "PostgreSQL"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    m = re.search(r"redis[_\s]*([\d.]+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in db_rows if r["vendor"] == "Redis"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    m = re.search(r"maria\s*db[_\s]*([\d.]+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in db_rows if r["vendor"] == "MariaDB"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    m = re.search(r"mysql[_\s]*([\d.]+)", text, re.I)
    if m:
        hit = _best_by_version([r for r in db_rows if r["vendor"] == "MySQL"], _parse_version_tuple(m.group(1)))
        return hit["eos_date"] if hit else None

    return None  # SAPHANA, 버전 없는 'MySQL' 단독 표기 등은 매칭 불가
