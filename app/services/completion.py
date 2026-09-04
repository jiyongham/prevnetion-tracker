# app/services/completion.py
import calendar
import re
from datetime import date

from app.config import settings
from app.core.date_utils import half_window, parse_schedule, parse_jira_date
from app.core.excel_loader import get_targets

DONE_MARKS = {"O", "0", "완료", "Y", "YES", "DONE"}

_CMDB_KEY_PATTERN = re.compile(r"\(([A-Z]+-\d+)\)\s*$")


_CMDB_OLD_PATTERN = re.compile(r"_OLD\s*$", re.IGNORECASE)


def _extract_cmdb_keys(raw: list | None) -> set[str]:
    """'작업 완료(CMDB)' 필드(예: '[시스템명]_OLD (ASSET-00000)') -> {Insight Key, ...}"""
    if not raw:
        return set()
    keys = set()
    for entry in raw:
        m = _CMDB_KEY_PATTERN.search(str(entry))
        if m:
            keys.add(m.group(1))
    return keys


def _extract_cmdb_old_keys(raw: list | None) -> set[str]:
    """
    '작업 완료(CMDB)' 필드 중 이름이 '_OLD'로 끝나는 항목의 Insight Key만.

    전환이 실제로 끝나면 AS-IS 자산명에 '_OLD'가 붙는데, 이 필드는 그 이름을 Key와
    함께 담고 있어(예: '[관계사,인프라] 서비스명 AP #1 (Active)_old (ASSET-00000)') 이름
    매칭 없이 "이 대상은 전환이 끝났다"를 정확히 알 수 있다. 같은 티켓 안에 전환 후
    신규 자산(접미사 없음)도 같이 들어있어 '_OLD'인 것만 골라야 한다.
    """
    if not raw:
        return set()
    keys = set()
    for entry in raw:
        text = str(entry)
        m = _CMDB_KEY_PATTERN.search(text)
        if m and _CMDB_OLD_PATTERN.search(_CMDB_KEY_PATTERN.sub("", text).strip()):
            keys.add(m.group(1))
    return keys


_MONTH_ONLY_RE = re.compile(r"(\d{1,2})\s*월")


def approx_schedule(raw: str, year: int) -> date | None:
    """
    '11월 예정', '12월'처럼 월만 적혀 날짜로 못 읽은 일정의 '정렬용' 근사 날짜.
    그 달 말일로 잡는다 - 그래야 같은 달의 확정 일정보다 뒤에 오고, 다음 달 일정보다
    앞에 온다. 완료 판정에는 쓰지 않고 화면 정렬에만 쓴다.
    '10월 → 11월'처럼 여러 달이 적혔으면 마지막(최신) 값을 쓴다.
    """
    months = _MONTH_ONLY_RE.findall(raw or "")
    if not months:
        return None
    month = int(months[-1])
    if not 1 <= month <= 12:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def fmt_rate(rate: float) -> str:
    """100.0 -> '100', 5.0 -> '5', 4.5 -> '4.5' (리포트 진행률 표시용, DR/용량관리 공용)"""
    return f"{rate:g}"


def ticket_kind(f: dict) -> str:
    """
    티켓 종류 판별.
    - 실전환 티켓 제목에 "예방3" 포함
    - 무중단 티켓 제목에 "무중단" 포함 (예방3 없음)
    - 제목에 둘 다 없어도, 작업 구분 필드에 "DR훈련"이 체크돼 있으면 실전환으로 기본 처리
      (전환기: 아직 이 필드를 안 쓰는 티켓이 대부분이라, 제목 태그를 우선 신뢰하고 이 필드는 보조로만 씀)
    """
    summary = f.get("summary", "") or ""
    if "예방3" in summary:
        return "실전환"
    if "무중단" in summary:
        return "무중단"
    work_types = {opt.get("value") for opt in (f.get(settings.dr_work_type_field) or [])}
    if "DR훈련" in work_types:
        return "실전환"
    return "기타"


def build_ticket_summary(issues: list[dict], field_id: str, kind_fn=ticket_kind, cmdb_field: str | None = None) -> list[dict]:
    """
    JIRA 원본 -> 필요 필드만 (kind_fn: 티켓 종류 판별 함수(fields dict를 받음), 기본은 DR훈련용)
    cmdb_field: '작업 완료(CMDB)'류 필드 ID를 주면 그 안의 Insight Key들을 cmdb_keys로 뽑아준다
    (변경작업내용 텍스트에 호스트명/IP가 없는 티켓도 이 필드로 정확히 매칭하기 위함 - EoS 전용)
    """
    result = []
    for issue in issues:
        f = issue["fields"]
        summary = f.get("summary", "")
        # 매칭용 통합 텍스트: 제목 + 본문 + 변경작업 대상 등 (호스트명/IP 포함)
        extra = "\n".join(str(f.get(k) or "") for k in settings.match_field_list)
        match_text = f"{summary}\n{f.get('description') or ''}\n{extra}"
        result.append({
            "key": issue["key"],
            "summary": summary,
            "kind": kind_fn(f),
            "description": f.get("description") or "",
            "match_text": match_text,
            "cmdb_keys": _extract_cmdb_keys(f.get(cmdb_field)) if cmdb_field else set(),
            "cmdb_old_keys": _extract_cmdb_old_keys(f.get(cmdb_field)) if cmdb_field else set(),
            "status": f["status"]["name"],
            "planned_end_date": parse_jira_date(f.get(field_id)),
            "planned_start_date": parse_jira_date(f.get(settings.planned_start_date_field)),
            "created": f.get("created", ""),                 # 원본(정렬용, ISO)
            "created_date": parse_jira_date(f.get("created")),  # 날짜(반기 창 판정용)
            "jsm_requester": (f.get(settings.jsm_requester_field) or {}).get("displayName", ""),
        })
    return result


def pick_display_ticket(tickets: list[dict]) -> dict | None:
    """표시용 대표 티켓: 실전환(최신 생성) > 무중단(최신 생성) > 아무거나(최신 생성)"""
    if not tickets:
        return None
    real = [t for t in tickets if t.get("kind") == "실전환" and t.get("planned_end_date")]
    if real:
        return max(real, key=lambda x: x.get("created") or "")
    nonstop = [t for t in tickets if t.get("kind") == "무중단"]
    if nonstop:
        return max(nonstop, key=lambda x: x.get("created") or "")
    return max(tickets, key=lambda x: x.get("created") or "")


def ticket_done_date(t: dict) -> date | None:
    """
    티켓의 '완료로 볼 날짜'.
    - 실전환: 변경계획완료일 (planned_end_date)
    - 무중단: 생성일 (created_date) — 무중단 티켓엔 완료일이 없음
    """
    if t.get("kind") == "실전환":
        return t.get("planned_end_date")
    if t.get("kind") == "무중단":
        return t.get("created_date")
    return None


def excluded_completion_date(item: dict, half_end: date) -> date | None:
    """
    '제외 확정' 대상의 완료 인정일. 제외 건이 아니면 None.

    제외는 관리자가 일정 칸에 X를 넣어 확정한다(웹 제외 버튼도 같은 값을 쓴다).
    비관리자가 넣은 X나 엑셀 원본에 그냥 적힌 X는 누가 왜 뺐는지 확인할 수 없으므로
    제외로 보지 않는다 - 대시보드의 excluded_nos 와 같은 기준이다.

    인정일은 '반기 종료일'과 '제외 처리일' 중 늦은 쪽이다.
      - 반기 안에 제외한 건  -> 반기 종료일 (H2면 12/31)
      - 반기가 끝난 뒤 제외한 건 -> 제외한 그 날
    반기 도중에는 아직 미완료로 남겨 두어야 하고(훈련을 더 할 수도 있다), 반대로
    반기가 끝난 뒤 새로 제외한 건을 12/31로 소급하면 이미 나간 결산 숫자와 어긋난다.
    """
    if (item.get("schedule_raw") or "").strip().upper() != "X":
        return None
    if item.get("updated_by") not in settings.admin_set:
        return None

    # updated_at 은 SQLite 의 'YYYY-MM-DD HH:MM:SS' 문자열. parse_jira_date 는 앞 10자리를
    # 날짜로 읽으므로 그대로 쓸 수 있다 (이름만 JIRA 일 뿐 형식은 같다).
    excluded_on = parse_jira_date(item.get("updated_at") or "")
    return max(half_end, excluded_on) if excluded_on else half_end


def judge(
    item: dict, tickets: list[dict] | None, as_of: date, base_year: int
) -> tuple[bool, str, dict | None]:
    """
    완료 판정 (JIRA 티켓 기준). 반환: (완료여부, 사유, 선택된 티켓)
    1) 엑셀/웹 완료 표기 O (수동 완료)
    2) 제외 확정 대상 (excluded_completion_date 참고) - 인정일 이후
    3) JIRA 티켓 (해당 반기 창 안 + 기준일 이전)
       - 실전환(예방3) 우선: 실전환 되면 무중단 불필요
       - 무중단(제목 "무중단"): 실전환 없을 때만
       - 같은 종류 여러 건이면 가장 최근 "생성된" 티켓 선택 (완료일 아님)
    ※ 예정일 경과 fallback 없음 (일정만 지나면 완료되던 로직 제거)
    """
    if item.get("excel_done", "").upper() in DONE_MARKS:
        return True, "완료표기", None

    start, end = half_window(base_year, item.get("half", "H2"))

    # 제외 확정 대상은 인정일이 되면 완료로 잡는다 (JIRA 티켓보다 먼저 본다 -
    # 제외된 대상에 티켓이 붙는 경우는 없고, 붙어도 판정 결과는 같다).
    if (done_on := excluded_completion_date(item, end)) and as_of >= done_on:
        return True, f"제외 확정 ({done_on})", None

    def in_window(t):
        d = ticket_done_date(t)
        return d and start <= d <= end and d <= as_of

    tks = tickets or []

    # 실전환(예방3) 우선: 여러 건이면 가장 최근 생성된 티켓
    real = [t for t in tks if t.get("kind") == "실전환" and in_window(t)]
    if real:
        t = max(real, key=lambda x: x.get("created") or "")
        return True, f"JIRA {t['key']} 실전환 ({t['planned_end_date']})", t

    # 무중단: 생성일이 최신인 티켓
    nonstop = [t for t in tks if t.get("kind") == "무중단" and in_window(t)]
    if nonstop:
        t = max(nonstop, key=lambda x: x.get("created") or "")
        return True, f"JIRA {t['key']} 무중단 (생성 {t['created_date']})", t

    return False, "", None


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
        matched = ticket_map.get(item["no"]) or []
        completed, reason, sel = judge(item, matched, as_of, year)
        if completed:
            done += 1

        # 표시용 티켓도 완료판정과 동일하게 '올해(base_year) 해당 반기' 것만
        w_start, w_end = half_window(year, item.get("half", "H2"))
        in_window = [
            t for t in matched
            if (dd := ticket_done_date(t)) and w_start <= dd <= w_end
        ]
        # 완료근거 티켓 우선, 없으면 창 안 매칭 티켓 중 대표
        display_ticket = sel or pick_display_ticket(in_window)

        sched = parse_schedule(item.get("schedule_raw", ""), year)
        # 계획일은 잡혔지만 그 날짜가 지나도록 완료(JIRA 티켓)가 안 됐으면
        # 계획이 무산된 것으로 보고 재계획이 필요한 '미계획'으로 재분류
        overdue_unfulfilled = bool(sched) and sched < as_of and not completed
        planned = bool(sched) and not overdue_unfulfilled

        # 화면 표시용 5분류. planned 하나로는 '아직 안 온 일정(예정)', '놓친 일정(지연)',
        # "'12월'처럼 월만 적힌 대략적 일정(대략)", '아무것도 없음(미계획)'이 구분되지 않는다.
        # planned/no_schedule의 의미는 그대로 둔다 - 리마인드 대상 산정이 그 값을 쓴다.
        # ('대략'도 확정 날짜가 필요하므로 리마인드 대상에는 계속 포함된다)
        if completed:
            status_label = "완료"
        elif not sched:
            # 날짜로 못 읽었지만 텍스트라도 적혀 있으면 '아무 계획 없음'과는 다르다
            status_label = "대략" if (item.get("schedule_raw") or "").strip() else "미계획"
        elif overdue_unfulfilled:
            status_label = "지연"
        else:
            status_label = "예정"

        # 정렬용 날짜. '대략'은 월만 알므로 그 달 말일로 근사해 '예정'과 같은 축에 놓는다
        sort_date = sched or (
            approx_schedule(item.get("schedule_raw"), year) if status_label == "대략" else None
        )

        # 이 시스템에 연결된(IP/호스트명 매칭) 티켓 중 가장 최근 생성된 것의 JSM요청자.
        # 미계획 리마인드에서 여러 담당자 후보 중 누구를 1순위로 볼지 판단하는 데 쓰인다.
        most_recent = max(matched, key=lambda t: t.get("created") or "") if matched else None
        jsm_requester = (most_recent or {}).get("jsm_requester", "")

        details.append({
            "no": item["no"],
            "company": item["company"],
            "business_name": item.get("business_name", ""),
            "system_name": item["system_name"],
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "owner": item["owner"],
            "jsm_requester": jsm_requester,
            "schedule_raw": item["schedule_raw"],
            "schedule": sched,
            # 표시용 일정: M/D로 통일 (엑셀 날짜형/텍스트형 혼재 정규화)
            "schedule_disp": f"{sched.month}/{sched.day}" if sched else (item["schedule_raw"] or ""),
            "planned": planned,  # 일정 없거나, 계획일 경과 후 미완료면 미계획
            "status_label": status_label,  # 완료 / 예정 / 지연 / 대략 / 미계획
            "schedule_sort": sort_date,    # 화면 정렬 전용 (대략은 월말로 근사)
            "mode": item["mode"],
            "jira_key": display_ticket["key"] if display_ticket else "",
            "jira_keys": [t["key"] for t in in_window],
            "jira_matched": bool(in_window),
            "completed": completed,
            "reason": reason,
            "input_source": item.get("input_source", "excel"),
            "updated_by": item.get("updated_by", ""),
            "updated_at": item.get("updated_at", ""),
            "evidence": item.get("evidence", ""),
            "note": item.get("note", ""),
            "exclude_reason": item.get("exclude_reason", ""),        # 엑셀 원본 사유
            "web_exclude_reason": item.get("web_exclude_reason", ""),  # 웹 제외 처리 사유
        })

    total = len(targets)
    return {
        "as_of": as_of,
        "half": items[0]["half"] if items else "",
        "total": total,
        "done": done,
        "rate": round(done / total * 100, 1) if total else 0.0,
        "no_schedule": len([d for d in details if not d["planned"]]),
        "scheduled": len([d for d in details if d["status_label"] == "예정"]),
        "overdue": len([d for d in details if d["status_label"] == "지연"]),
        "approximate": len([d for d in details if d["status_label"] == "대략"]),
        "unplanned": len([d for d in details if d["status_label"] == "미계획"]),
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
