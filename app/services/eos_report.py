# app/services/eos_report.py
import re
from datetime import date

from app.config import settings
from app.core.date_utils import week_ranges
from app.core.eos_loader import get_targets
from app.core.teams_client import send_teams_message
from app.services import last_report
from app.services.report_check import check_report, record_sent
from app.models.db import get_eos_next_week_plan
from app.services.eos import calc_eos_completion, filter_track
from app.services.eos_confluence import get_week_plan_count
from app.services.eos_data import get_eos_data

# OS/DB 트랙 전체 대상 수는 프로젝트 착수 시점에 고정된 모수 (엑셀 행 수와 별개 - 지시에 따라 계산하지 않고 고정값 사용)
OS_TOTAL_FIXED = 384
DB_TOTAL_FIXED = 49


def collect_eos(use_jira: bool = True):
    """
    반환: (items, ticket_map, polestar_confirmed)
    수집 자체는 대시보드와 공용(eos_data.get_eos_data) - 두 곳이 따로 구현해 숫자가
    어긋나는 걸 막기 위함. 주간 리포트는 발송 시점의 최신값을 써야 하므로 캐시를 무시한다.
    """
    items, ticket_map, polestar_confirmed, _ = get_eos_data(
        use_external=use_jira, force_refresh=True
    )
    return items, ticket_map, polestar_confirmed


_MONTHS_RE = re.compile(r"(\d{1,2})\s*월")


def _moved_in(schedule_raw: str, month: int) -> bool:
    """
    '10월 → 8월'처럼 다른 달에서 이번 달로 옮겨온 대상인지.
    '8월  -->> 완료예정'은 화살표가 있어도 앞쪽이 같은 달이라 일정 변경이 아니다.
    """
    months = [int(m) for m in _MONTHS_RE.findall(schedule_raw or "")]
    return len(set(months)) > 1 and months[-1] == month


def _month_stats(details: list[dict], today: date) -> tuple[int, int, int]:
    """이번 달 목표 대상 수 / 그 중 완료 수 / 일정 변경으로 이번 달에 들어온 수"""
    this_month = [
        d for d in details
        if d["schedule"] and d["schedule"].year == today.year and d["schedule"].month == today.month
    ]
    done = sum(1 for d in this_month if d["completed"])
    moved = sum(1 for d in this_month if _moved_in(d.get("schedule_raw", ""), today.month))
    return len(this_month), done, moved


def _track_section(
    title: str,
    section_no: int,
    fixed_total: int,
    items: list[dict],
    ticket_map: dict,
    today: date,
    perf_start: date, perf_end: date,
    plan_start: date, plan_end: date,
    polestar_confirmed: set[str] | None = None,
    perf_matched: dict | None = None,
    plan_saved: dict | None = None,
) -> tuple[list[str], dict]:
    result = calc_eos_completion(items, ticket_map, today, polestar_confirmed=polestar_confirmed)
    rate = round(result["done"] / fixed_total * 100) if fixed_total else 0

    month_target, month_done, month_moved = _month_stats(result["details"], today)
    month_rate = round(month_done / month_target * 100) if month_target else 0

    # perf_matched/plan_saved는 전체 대상 기준으로 한 번만 조회된 것 - 이 트랙(OS/DB)
    # 소속 item_no로만 걸러서 센다.
    track_item_nos = {d["item_no"] for d in result["details"]}
    perf_cnt = len(track_item_nos & (perf_matched or {}).keys())
    plan_cnt = len(track_item_nos & (plan_saved or {}).keys())

    lines = [
        f"[{title}]",
        f"{section_no}. `26년 하반기 EoS 버전 업그레이드 진행",
        f"   1) 총 {fixed_total}대 中 {result['done']}대 완료 (진행률 {rate}%)",
        "   2) 월별 목표",
        f"       - {today.month}월 목표 {month_target}대 中 {month_done}대 완료 (진행률 {month_rate}%)",
    ]
    if month_moved:
        lines.append(
            f"         ※ 일정 변경으로 {today.month}월 목표 추가 {month_moved}대 "
            f"(기존 {month_target - month_moved}대)"
        )
    lines += [
        "   3) 실적",
        f"    - 금주 실적 ({perf_start:%m/%d} ~ {perf_end:%m/%d}) : {perf_cnt}대",
        f"    - 차주 계획 ({plan_start:%m/%d} ~ {plan_end:%m/%d}) : {plan_cnt}대",
        "",
    ]

    # 발송 전 점검용 집계. 분모는 리포트에 실제로 쓰는 고정값(fixed_total)으로 맞춘다 -
    # 화면에 나간 숫자와 다른 값으로 비교하면 경고가 엉뚱하게 뜬다.
    metrics = {
        "total": fixed_total,
        "done": result["done"],
        "rate": rate,
        "no_schedule": result["no_schedule"],
        "perf_cnt": perf_cnt,
        "plan_cnt": plan_cnt,
        "composition": {f"{title} 월별": {f"{today.month}월 목표": month_target, f"{today.month}월 완료": month_done}},
    }
    return lines, metrics


def build_eos_report(use_jira: bool = True) -> tuple[str, dict]:
    today = date.today()
    perf_start, perf_end, plan_start, plan_end = week_ranges(today)

    items, ticket_map, polestar_confirmed = collect_eos(use_jira)
    os_items = filter_track(items, "OS")
    db_items = filter_track(items, "DB")
    all_targets = get_targets(items)

    # 금주 실적(발송 기준 차주): JIRA에 티켓이 아직 안 잡힌 작업도 Confluence 주간계획
    # 페이지엔 미리 올라오는 경우가 많아, JIRA 대신 Confluence 파싱 기준으로 센다.
    perf_matched: dict = {}
    try:
        perf_result = get_week_plan_count(
            settings.confluence_eos_parent_page_id, perf_start, perf_end, all_targets
        )
        perf_matched = perf_result["matched"]
    except Exception as e:
        print(f"⚠️ EoS Confluence 조회 실패 (금주 실적 0으로 표시): {e}")

    # 관리자가 챗봇(/eos/plan-chat)으로 확인해서 수동 입력해둔 값.
    # - 차주 계획: 아직 취합 자체가 어려운 시점이라 이 값이 유일한 근거다.
    # - 금주 실적: Confluence 집계를 '보완'한다. 작업계획서 PDF가 리포트 발송 뒤에
    #   올라오는 경우가 있어, 그런 건은 담당자가 아는 대로 넣어 합집합으로 센다.
    plan_saved = get_eos_next_week_plan(plan_start.isoformat())
    perf_saved = get_eos_next_week_plan(perf_start.isoformat(), kind="perf")
    perf_matched = {**perf_matched, **perf_saved}

    lines = ["[EoS]", ""]
    os_lines, os_metrics = _track_section(
        "OS", 1, OS_TOTAL_FIXED, os_items, ticket_map, today, perf_start, perf_end,
        plan_start, plan_end, polestar_confirmed, perf_matched, plan_saved,
    )
    db_lines, db_metrics = _track_section(
        "DB", 2, DB_TOTAL_FIXED, db_items, ticket_map, today, perf_start, perf_end,
        plan_start, plan_end, polestar_confirmed, perf_matched, plan_saved,
    )
    lines += os_lines + db_lines

    # 트랙별로 따로 점검한다. OS/DB를 합쳐서 보면 한쪽이 0이 돼도 다른 쪽에 묻힌다.
    return "\n".join(lines), {"eos_os": os_metrics, "eos_db": db_metrics}


def send_eos_report(use_jira: bool = True) -> str | None:
    """
    리포트 발송. 발송 전 지난주 대비 이상 징후를 점검하고, 이상이 있어도 정기 보고라
    발송 자체는 막지 않고 경고만 반환한다 (DR훈련/용량관리와 동일).

    특히 완료 대수 감소를 여기서 잡는다 - EoS 완료 근거 중 Polestar '_OLD'는 CI가
    폐기되면 사라질 수 있어(eos_data.merge_polestar_latch가 기록으로 막고 있지만),
    다른 소스가 조용히 실패해도 같은 증상이 나온다.
    """
    text, metrics = build_eos_report(use_jira=use_jira)

    warnings = [w for w in (check_report(d, m) for d, m in metrics.items()) if w]
    warning = "\n".join(warnings) if warnings else None

    print(text)
    if warning:
        print(warning)
    print("-" * 60)
    send_teams_message(text)
    last_report.remember("eos", text)   # 발송 직후 화면에서 확인할 수 있게
    for domain, m in metrics.items():
        record_sent(domain, m)
    return warning
