# app/services/completion.py
from datetime import date

from app.core.date_utils import parse_schedule, parse_jira_date
from app.core.excel_loader import get_targets

DONE_MARKS = {"O", "0", "완료", "Y", "YES", "DONE"}


def build_ticket_summary(issues: list[dict], field_id: str) -> list[dict]:
    """JIRA 원본 -> 필요 필드만"""
    result = []
    for issue in issues:
        f = issue["fields"]
        result.append({
            "key": issue["key"],
            "summary": f.get("summary", ""),
            "description": f.get("description") or "",
            "status": f["status"]["name"],
            "planned_end_date": parse_jira_date(f.get(field_id)),
        })
    return result


def judge(item: dict, ticket: dict | None, as_of: date, base_year: int) -> tuple[bool, str]:
    """
    완료 판정 (우선순위)
    1) 엑셀 완료 표기 O
    2) JIRA 변경계획완료일 <= 기준일
    3) 엑셀 일정 <= 기준일 (JIRA 미매칭 시 fallback)
    """
    if item.get("excel_done", "").upper() in DONE_MARKS:
        return True, "엑셀 완료표기"

    if ticket and ticket.get("planned_end_date"):
        if ticket["planned_end_date"] <= as_of:
            return True, f"JIRA {ticket['key']} ({ticket['planned_end_date']})"

    sched = parse_schedule(item.get("schedule_raw", ""), base_year)
    if sched and sched <= as_of:
        return True, f"엑셀 일정 경과 ({sched})"

    return False, ""


def calc_completion(
    items: list[dict],
    ticket_map: dict[str, dict],
    as_of: date,
    base_year: int | None = None,
) -> dict:
    """완료율 계산 (대상 O만 분모)"""
    year = base_year or as_of.year
    targets = get_targets(items)

    done = 0
    details = []

    for item in targets:
        ticket = ticket_map.get(item["no"])
        completed, reason = judge(item, ticket, as_of, year)
        if completed:
            done += 1

        sched = parse_schedule(item.get("schedule_raw", ""), year)

        details.append({
            "no": item["no"],
            "company": item["company"],
            "system_name": item["system_name"],
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "owner": item["owner"],
            "schedule_raw": item["schedule_raw"],
            "schedule": sched,
            "mode": item["mode"],
            "jira_key": ticket["key"] if ticket else "",
            "completed": completed,
            "reason": reason,
        })

    total = len(targets)
    return {
        "as_of": as_of,
        "half": items[0]["half"] if items else "",
        "total": total,
        "done": done,
        "rate": round(done / total * 100, 1) if total else 0.0,
        "no_schedule": len([d for d in details if not d["schedule"]]),
        "details": details,
    }


def group_by(result: dict, key: str = "ops_team") -> dict:
    """팀별/관계사별 집계"""
    groups = {}
    for d in result["details"]:
        k = d.get(key) or "미지정"
        groups.setdefault(k, {"total": 0, "done": 0, "pending": []})
        groups[k]["total"] += 1
        if d["completed"]:
            groups[k]["done"] += 1
        else:
            groups[k]["pending"].append(d)

    for v in groups.values():
        v["rate"] = round(v["done"] / v["total"] * 100, 1) if v["total"] else 0.0

    return groups
