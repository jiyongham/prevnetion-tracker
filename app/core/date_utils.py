# app/core/date_utils.py
import re
from datetime import date, datetime

# 6/15, 6-15, 6.15, 6월 15일, 2025-06-15 등 대응
PATTERNS = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})$"), "md"),
    (re.compile(r"^(\d{1,2})월\s*(\d{1,2})일?$"), "md"),
]


def parse_schedule(value: str, base_year: int | None = None) -> date | None:
    """
    엑셀 일정 문자열 -> date
    '6/15' -> 2025-06-15 (연도 없으면 base_year 사용)
    """
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    year = base_year or date.today().year

    for pattern, kind in PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        try:
            if kind == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:  # md
                return date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None

    # pandas가 datetime으로 읽은 경우
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def parse_jira_date(value) -> date | None:
    """JIRA 날짜 필드 -> date"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
