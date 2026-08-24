# app/services/eos.py
from datetime import date

from app.config import settings
from app.core.date_utils import half_window, parse_schedule
from app.core.eos_loader import get_targets, parse_eos_schedule
from app.services.completion import DONE_MARKS, build_ticket_summary


def eos_ticket_kind(f: dict) -> str:
    """
    작업 구분 판별. "IP전환"(또는 "IP 전환")만 완료 판정에 쓰고, "생성"은 참고용일 뿐
    (신규 VM/DB 생성만 했다고 EoS 전환이 끝난 게 아니라 IP전환까지 돼야 완료).
    """
    s = f.get("summary", "") or ""
    if "IP전환" in s or "IP 전환" in s:
        return "IP전환"
    if "생성" in s:
        return "생성"
    return "기타"


def build_eos_ticket_summary(issues: list[dict], field_id: str) -> list[dict]:
    """JIRA 원본 -> 필요 필드만 (kind 판별은 IP전환/생성 기준, 매칭은 작업 완료(CMDB) 필드 Key 기준)"""
    return build_ticket_summary(issues, field_id, kind_fn=eos_ticket_kind, cmdb_field=settings.eos_cmdb_done_field)


def filter_track(items: list[dict], track: str) -> list[dict]:
    """
    '제품별 EoS 일정' 표로 판별한 os_eos_target/db_eos_target 기준으로 트랙별 대상만 골라낸다.
    같은 target이어도 OS만 EoS 대상이고 DB는 아직 아닌 경우가 있어(반대도 마찬가지),
    calc_eos_completion이 보는 is_target을 트랙 기준으로 다시 씌운 사본을 만든다.
    track이 "OS"/"DB"가 아니면(예: "ALL") 원본 그대로 반환.
    """
    if track not in ("OS", "DB"):
        return items
    field = "os_eos_target" if track == "OS" else "db_eos_target"
    result = []
    for i in items:
        i2 = dict(i)
        i2["is_target"] = bool(i["is_target"] and i.get(field))
        result.append(i2)
    return result


def build_no_reply_details(items: list[dict], base_year: int) -> list[dict]:
    """
    '미응답'(EOS 진행/제외 여부 자체가 미기입) 대상을 상세 목록에 같이 보여주기 위한 변환.
    완료율(분모/분자)에는 안 들어가고 - 여전히 EOS 진행(target) 대상 기준 - 화면 상세
    목록에서만 나머지 대상들과 나란히 보여준다. status 배지는 무조건 "미응답"으로 표시.
    """
    result = []
    for item in items:
        sched = parse_eos_schedule(item.get("schedule_raw", ""), base_year)
        result.append({
            "item_no": item["item_no"],
            "insight_key": item["insight_key"],
            "object_type": item["object_type"],
            "system_name": item["system_name"],
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "owner": item["owner"],
            "jsm_requester": "",
            "center": item["center"],
            "os": item["os"],
            "db": item["db"],
            "infra_type": item["infra_type"],
            "schedule_raw": item.get("schedule_raw", ""),
            "schedule": sched,
            "schedule_disp": sched.strftime("%Y-%m") if sched else (item.get("schedule_raw") or ""),
            "planned": False,
            "jira_key": "",
            "jira_matched": False,
            "completed": False,
            "reason": "",
            "input_source": item.get("input_source", "excel"),
            "updated_by": item.get("updated_by", ""),
            "updated_at": item.get("updated_at", ""),
            "evidence": item.get("evidence", ""),
            "note": item.get("note", ""),
            "exclude_reason": item.get("exclude_reason", ""),
            "no_reply": True,
        })
    return result


def eos_ticket_done_date(t: dict) -> date | None:
    """완료로 볼 날짜: 변경계획시작일 (IP전환 작업만 인정, 생성은 참고만 하고 완료로 안 침)"""
    if t.get("kind") == "IP전환":
        return t.get("planned_start_date")
    return None


def judge_eos(
    item: dict, tickets: list[dict] | None, as_of: date, base_year: int
) -> tuple[bool, str, dict | None]:
    """
    완료 판정. 엑셀엔 완료 컬럼이 없어 (1) 관리자가 웹에서 수동 완료 처리했거나
    (2) 매칭된 [예방1] 티켓 중 "IP전환" 작업의 변경계획시작일이 하반기 종료 전 + 기준일 이전인 경우만 완료.

    하반기 시작(7/1) 이전에 조기 완료한 건도 인정한다 (하한 없이 종료일만 체크) -
    실제로 하반기 목표를 앞당겨 6월에 IP전환까지 끝내는 경우가 있어, 시작일 하한을
    두면 이미 끝난 작업이 "미완료"로 오판된다.
    """
    if (item.get("excel_done") or "").upper() in DONE_MARKS:
        return True, "완료표기", None

    _, end = half_window(base_year, "H2")
    tks = tickets or []
    in_window = [
        t for t in tks
        if (d := eos_ticket_done_date(t)) and d <= end and d <= as_of
    ]
    if in_window:
        t = max(in_window, key=lambda x: x.get("created") or "")
        return True, f"JIRA {t['key']} IP전환완료 ({t['planned_start_date']})", t

    return False, "", None


def calc_eos_completion(
    items: list[dict],
    ticket_map: dict[str, list[dict]],
    as_of: date,
    base_year: int | None = None,
) -> dict:
    """완료율 계산 (EOS 진행(target) 대상만 분모). ticket_map은 item_no(Insight Key) 기준."""
    year = base_year or as_of.year
    targets = get_targets(items)
    _, w_end = half_window(year, "H2")

    done = 0
    details = []

    for item in targets:
        matched = ticket_map.get(item["item_no"]) or []
        completed, reason, sel = judge_eos(item, matched, as_of, year)
        if completed:
            done += 1

        in_window = [
            t for t in matched
            if (dd := eos_ticket_done_date(t)) and dd <= w_end
        ]
        display_ticket = sel or (
            max(in_window, key=lambda x: x.get("created") or "") if in_window else None
        )

        sched = parse_eos_schedule(item.get("schedule_raw", ""), year)
        overdue_unfulfilled = bool(sched) and sched < as_of and not completed
        planned = bool(sched) and not overdue_unfulfilled

        # 이 시스템에 연결된(IP/호스트명 매칭) 티켓 중 가장 최근 생성된 것의 JSM요청자.
        # 미계획 리마인드에서 여러 담당자 후보 중 누구를 1순위로 볼지 판단하는 데 쓰인다.
        most_recent = max(matched, key=lambda t: t.get("created") or "") if matched else None
        jsm_requester = (most_recent or {}).get("jsm_requester", "")

        details.append({
            "item_no": item["item_no"],
            "insight_key": item["insight_key"],
            "object_type": item["object_type"],
            "system_name": item["system_name"],
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "owner": item["owner"],
            "jsm_requester": jsm_requester,
            "center": item["center"],
            "os": item["os"],
            "db": item["db"],
            "infra_type": item["infra_type"],
            "schedule_raw": item["schedule_raw"],
            "schedule": sched,
            "schedule_disp": sched.strftime("%Y-%m") if sched else (item["schedule_raw"] or ""),
            "planned": planned,
            "jira_key": display_ticket["key"] if display_ticket else "",
            "jira_matched": bool(in_window),
            "completed": completed,
            "reason": reason,
            "input_source": item.get("input_source", "excel"),
            "updated_by": item.get("updated_by", ""),
            "updated_at": item.get("updated_at", ""),
            "evidence": item.get("evidence", ""),
            "note": item.get("note", ""),
            "exclude_reason": item.get("exclude_reason", ""),
        })

    total = len(targets)
    return {
        "as_of": as_of,
        "total": total,
        "done": done,
        "rate": round(done / total * 100, 1) if total else 0.0,
        "no_schedule": len([d for d in details if not d["planned"]]),
        "details": details,
    }
