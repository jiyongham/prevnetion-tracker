# app/core/date_utils.py
import re
from datetime import date, datetime, timedelta

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


def half_window(year: int, half: str) -> tuple[date, date]:
    """
    반기 완료 인정 구간.
    - 연 1회 실전환 필수 → 해당 연도의 반기 안에 완료된 것만 인정
    - H1: 1/1~6/30, H2: 7/1~12/31
    """
    if half == "H1":
        return date(year, 1, 1), date(year, 6, 30)
    return date(year, 7, 1), date(year, 12, 31)


def week_ranges(today: date):
    """
    발송일(목요일) 기준 주간 구간 (DR훈련/EoS 리포트 공용)
    - 금주 실적: 발송 다음 주 (월~금)
    - 차주 계획: 그 다음 주 (월~금)
    """
    this_monday = today - timedelta(days=today.weekday())
    perf_start = this_monday + timedelta(days=7)      # 금주 실적 (월)
    perf_end = perf_start + timedelta(days=4)          # (금)
    plan_start = perf_start + timedelta(days=7)        # 차주 계획 (월)
    plan_end = plan_start + timedelta(days=4)          # (금)
    return perf_start, perf_end, plan_start, plan_end


def parse_jira_date(value) -> date | None:
    """JIRA 날짜 필드 -> date"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
