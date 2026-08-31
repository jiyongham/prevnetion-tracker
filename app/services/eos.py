# app/services/eos.py
import re
from datetime import date

from app.config import settings
from app.core.date_utils import half_window
from app.core.eos_loader import _effective, get_targets, parse_eos_schedule
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
            "web_exclude_reason": item.get("web_exclude_reason", ""),
            "no_reply": True,
        })
    return result


# 실제로 전환이 이뤄지지 않은(취소/보류) 티켓 - 완료 근거로 쓰면 안 됨
_NOT_DONE_STATUSES = {"중단", "반려", "종료"}


def eos_ticket_done_date(t: dict) -> date | None:
    """완료로 볼 날짜: 변경계획시작일 (IP전환 작업만 인정, 생성은 참고만 하고 완료로 안 침. 중단/반려/종료된 티켓은 제외)"""
    if t.get("kind") == "IP전환" and t.get("status") not in _NOT_DONE_STATUSES:
        return t.get("planned_start_date")
    return None


def schedule_marks_done(raw: str) -> bool:
    """
    조치계획란에 담당자가 직접 적은 완료 표기인지.
    EoS 엑셀엔 완료 컬럼이 없어서 담당자가 조치계획란에 '7월 완료 (추가)',
    '9월 → 8월 (완료)'처럼 적는다. 티켓이 매칭 안 되는 추가 대상들이 여기 걸린다.

    - '→'가 있으면 최종값만 본다 (앞쪽 값이 번복된 것을 완료로 읽지 않기 위함)
    - '완료 예정'/'완료예정'은 아직 안 끝난 것이므로 제외
    """
    eff = _effective(raw or "")
    if "완료" not in eff:
        return False
    return not re.search(r"완료\s*예정", eff)


def judge_eos(
    item: dict,
    tickets: list[dict] | None,
    as_of: date,
    base_year: int,
    polestar_confirmed: set[str] | None = None,
) -> tuple[bool, str, dict | None]:
    """
    완료 판정. 아래 근거만 인정하며, 위에서부터 먼저 걸리는 것을 채택한다.

    1) 관리자가 웹에서 수동 완료 처리          → "완료표기"
    2) 매칭된 IP전환 티켓의 '작업 완료(CMDB)'에 이 대상이 '_OLD'로 기록됨
    3) Polestar CI가 '_OLD'로 리네임됨 (polestar_confirmed로 주입)
    4) 담당자가 조치계획란에 '완료'라고 적음 (엑셀에 완료 컬럼이 없어 여기 적는다)

    순서는 근거의 강도 순이다. 2)는 Insight Key로 대상이 특정돼 가장 정확하고, 3)은
    이름/IP 매칭이라 드리프트 여지가 있으며, 4)는 사람이 적은 텍스트라 마지막에 본다.
    앞선 근거가 있으면 화면에 근거 티켓까지 같이 보여줄 수 있는 이점도 있다.

    티켓이 매칭됐다는 것만으로는 완료로 보지 않는다. 변경계획시작일은 "작업을 시작하기로
    한 날"일 뿐이라, 계획일만 지나고 실제 전환은 안 끝난 건까지 완료로 세어 실측치보다
    높게 나왔다. 반대로 티켓 status='완료'만 세면 후속 단계(CMDB 업데이트/결과등록 대기)에
    걸린 건이 빠져 낮게 나온다. 그래서 "실제로 AS-IS가 _OLD로 바뀌었는가"를 본다.

    2)와 3)을 합집합으로 쓰는 이유: CMDB 필드가 비어 있는 티켓이 있고(작업자가 미기입),
    Polestar는 CI명 드리프트로 매칭이 안 되는 경우가 있어 서로를 보완한다.
    ※ 엑셀 시스템명이 이미 '_OLD'로 끝나는 대상은 3)의 판정 대상에서 빠진다
      (eos_polestar.judge_converted 참고 - 리네임 결과인지 원래 이름인지 구분 불가).
    """
    if (item.get("excel_done") or "").upper() in DONE_MARKS:
        return True, "완료표기", None

    start, end = half_window(base_year, "H2")
    tks = tickets or []
    in_window = [
        t for t in tks
        if (d := eos_ticket_done_date(t)) and start <= d <= end and d <= as_of
    ]

    for t in sorted(in_window, key=lambda x: x.get("created") or "", reverse=True):
        if item["item_no"] in (t.get("cmdb_old_keys") or set()):
            return True, f"JIRA {t['key']} CMDB 전환완료 ({t['planned_start_date']})", t

    if polestar_confirmed and item["item_no"] in polestar_confirmed:
        latest = max(in_window, key=lambda x: x.get("created") or "") if in_window else None
        return True, "Polestar CI _OLD 확인", latest

    # 최후 수단. 시스템 근거(JIRA/Polestar)가 우선이고, 그게 없을 때만 담당자 표기를 믿는다
    # - 표기보다 실제 자산 상태가 정확하고, 화면에 근거 티켓도 같이 보여줄 수 있기 때문.
    if schedule_marks_done(item.get("schedule_raw")):
        return True, f"조치계획란 완료표기 ({item.get('schedule_raw')})", None

    return False, "", None


def calc_eos_completion(
    items: list[dict],
    ticket_map: dict[str, list[dict]],
    as_of: date,
    base_year: int | None = None,
    polestar_confirmed: set[str] | None = None,
) -> dict:
    """
    완료율 계산 (EOS 진행(target) 대상만 분모). ticket_map은 item_no(Insight Key) 기준.
    polestar_confirmed: Polestar에서 '_OLD'로 확인된 item_no 집합
    (app.services.eos_polestar.confirmed_item_nos()로 만든다. 조회 실패 시 None을 넘기면
     JIRA CMDB 근거만으로 판정한다.)
    """
    year = base_year or as_of.year
    targets = get_targets(items)
    _, w_end = half_window(year, "H2")

    done = 0
    details = []

    for item in targets:
        matched = ticket_map.get(item["item_no"]) or []
        completed, reason, sel = judge_eos(item, matched, as_of, year, polestar_confirmed)
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
