# scripts/diag_perf_week.py
"""
'금주 실적'/'차주 계획' 집계 창 확인 + 특정 주간에 일정이 잡힌 대상의
JIRA 티켓 완료일 매칭 상태를 점검하는 진단 스크립트.

사용법:
  python -m scripts.diag_perf_week
"""
from datetime import date

from app.core.excel_loader import scope_h2_targets
from app.services.completion import calc_completion, ticket_done_date
from app.services.report import _week_ranges, collect


def main():
    today = date.today()
    perf_start, perf_end, plan_start, plan_end = _week_ranges(today)
    print(f"오늘: {today}")
    print(f"금주 실적 집계 창 (JIRA 완료일 기준): {perf_start} ~ {perf_end}")
    print(f"차주 계획 집계 창 (입력 일정 기준):    {plan_start} ~ {plan_end}")
    print()

    h2_items, h2_tmap = collect("H2", use_jira=True)
    h2_scope = scope_h2_targets(h2_items)
    result = calc_completion(h2_scope, h2_tmap, today)

    hits = [d for d in result["details"] if d["schedule"] and perf_start <= d["schedule"] <= perf_end]
    print(f"입력 일정이 금주 실적 구간({perf_start}~{perf_end})에 있는 대상: {hits and len(hits) or 0}건\n")

    for d in hits:
        tickets = h2_tmap.get(d["no"]) or []
        done_dates = [(t.get("key"), t.get("kind"), ticket_done_date(t)) for t in tickets]
        counted = any(
            (dd := ticket_done_date(t)) and perf_start <= dd <= perf_end
            for t in tickets
        )
        mark = "O 집계됨" if counted else "X 집계 안됨"
        print(
            f"[{mark}] NO.{d['no']} {d['system_name']} | 입력일정={d['schedule']} | "
            f"완료여부={d['completed']} | 매칭티켓={d['jira_keys']} | "
            f"티켓별(key, kind, done_date)={done_dates}"
        )


if __name__ == "__main__":
    main()
