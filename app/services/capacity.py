# app/services/capacity.py
import re
from datetime import date

from app.core.capacity_loader import get_targets
from app.core.date_utils import half_window, parse_schedule
from app.services.completion import DONE_MARKS, build_ticket_summary


def capacity_ticket_kind(f: dict) -> str:
    """티켓 종류 판별 - 용량관리(증설) 티켓은 제목에 "예방4" 포함"""
    summary = f.get("summary", "") or ""
    return "예방4" if "예방4" in summary else "기타"


def build_capacity_ticket_summary(issues: list[dict], field_id: str) -> list[dict]:
    """JIRA 원본 -> 필요 필드만 (kind 판별만 예방4 기준으로 다름)"""
    return build_ticket_summary(issues, field_id, kind_fn=capacity_ticket_kind)


# 변경작업내용(match_text) 안의 마운트/디스크그룹 표기로 DATA(일반)/ARCH(아카이브) 티켓 판별
# 같은 서버가 DATA·ARCH 양쪽 시트에 다 나오는 경우가 많아서, IP/호스트명만으로는 어느 쪽
# 작업인지 구분이 안 됨 -> 변경작업내용 텍스트로 소속 시트를 가려낸다.
# 주의: \bDATA/\bRECO는 뒤쪽 경계가 없어 "DATABASE"/"RECOVERY" 같은 일반 단어의 접두부에도
# 걸린다. 티켓 설명엔 "데이터베이스(Database)"가 거의 항상 들어가므로, 이 lookahead가 없으면
# 실제로는 ARCH(RECO) 작업인 티켓도 DATA로 오판정돼 조용히 걸러져버린다.
_DATA_PATTERNS = [re.compile(r"/oradata", re.I), re.compile(r"\bDATA(?!BASE)", re.I)]
_ARCH_PATTERNS = [re.compile(r"/arch", re.I), re.compile(r"\bRECO(?!VERY)", re.I)]


def classify_capacity_sheet(match_text: str) -> set[str]:
    """
    티켓의 변경작업내용에서 DATA/ARCH 여부 판별 (해당 없으면 빈 set).
    한 티켓에서 DATA·RECO(ASM) 영역을 같이 증설 요청하는 경우가 흔해서
    (예: "DATA 영역 2T, Arch 영역 1T 증설") 둘 다 걸릴 수 있어 set으로 반환한다 -
    예전처럼 DATA를 먼저 체크해서 하나만 반환하면, 두 영역을 같이 요청한 티켓이
    전부 DATA로만 잡히고 ARCH 쪽에서는 완전히 누락됐다.
    """
    text = match_text or ""
    sheets = set()
    if any(p.search(text) for p in _DATA_PATTERNS):
        sheets.add("DATA")
    if any(p.search(text) for p in _ARCH_PATTERNS):
        sheets.add("ARCH")
    return sheets


def filter_tickets_by_sheet(ticket_map: dict[str, list[dict]], sheet: str) -> dict[str, list[dict]]:
    """IP/호스트명으로 매칭된 티켓 중, 변경작업내용상 이 시트(DATA/ARCH) 소속인 것만 남긴다."""
    filtered = {}
    for no, tickets in ticket_map.items():
        keep = [t for t in tickets if sheet in classify_capacity_sheet(t.get("match_text"))]
        if keep:
            filtered[no] = keep
    return filtered


def capacity_ticket_done_date(t: dict) -> date | None:
    """완료로 볼 날짜: 변경계획완료일 (예방4는 실전환/무중단 구분 없음)"""
    if t.get("kind") == "예방4":
        return t.get("planned_end_date")
    return None


def judge_capacity(
    item: dict, tickets: list[dict] | None, as_of: date, base_year: int
) -> tuple[bool, str, dict | None]:
    """
    완료 판정 (JIRA [예방4] 티켓 기준).
    1) 엑셀/웹 '증설 완료' 표기
    2) 매칭된 [예방4] 티켓의 변경계획완료일이 하반기 창 안 + 기준일 이전
    """
    if (item.get("excel_done") or "").upper() in DONE_MARKS:
        return True, "완료표기", None

    start, end = half_window(base_year, "H2")
    tks = tickets or []
    in_window = [
        t for t in tks
        if (d := capacity_ticket_done_date(t)) and start <= d <= end and d <= as_of
    ]
    if in_window:
        t = max(in_window, key=lambda x: x.get("created") or "")
        return True, f"JIRA {t['key']} 증설완료 ({t['planned_end_date']})", t

    return False, "", None


def calc_capacity_completion(
    items: list[dict],
    ticket_map: dict[str, list[dict]],
    as_of: date,
    base_year: int | None = None,
) -> dict:
    """완료율 계산 (증설 여부 O만 분모). ticket_map은 item['no'](시트 내 번호) 기준."""
    year = base_year or as_of.year
    targets = get_targets(items)
    w_start, w_end = half_window(year, "H2")

    done = 0
    details = []

    for item in targets:
        matched = ticket_map.get(item["no"]) or []
        completed, reason, sel = judge_capacity(item, matched, as_of, year)
        if completed:
            done += 1

        in_window = [
            t for t in matched
            if (dd := capacity_ticket_done_date(t)) and w_start <= dd <= w_end
        ]
        display_ticket = sel or (
            max(in_window, key=lambda x: x.get("created") or "") if in_window else None
        )

        sched = parse_schedule(item.get("schedule_raw", ""), year)
        overdue_unfulfilled = bool(sched) and sched < as_of and not completed
        planned = bool(sched) and not overdue_unfulfilled

        # 이 시스템에 연결된(IP/호스트명 매칭) 티켓 중 가장 최근 생성된 것의 JSM요청자.
        # 미계획 리마인드에서 여러 담당자 후보 중 누구를 1순위로 볼지 판단하는 데 쓰인다.
        most_recent = max(matched, key=lambda t: t.get("created") or "") if matched else None
        jsm_requester = (most_recent or {}).get("jsm_requester", "")

        details.append({
            "item_no": item["item_no"],
            "sheet": item["sheet"],
            "no": item["no"],
            "ci_name": item["ci_name"],
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "owner": item["owner"],
            "jsm_requester": jsm_requester,
            "center": item["center"],
            "fs_type": item["fs_type"],
            "infra_type": item["infra_type"],
            "total_gb": item["total_gb"],
            "remaining_gb": item["remaining_gb"],
            "usage_pct": item["usage_pct"],
            "required_gb": item["required_gb"],
            "schedule_raw": item["schedule_raw"],
            "schedule": sched,
            "schedule_disp": f"{sched.month}/{sched.day}" if sched else (item["schedule_raw"] or ""),
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
