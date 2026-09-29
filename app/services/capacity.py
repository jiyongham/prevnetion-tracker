# app/services/capacity.py
import logging
import re
from datetime import date

from app.config import settings
from app.core.capacity_loader import get_targets
from app.core.date_utils import half_window, parse_jira_date, parse_schedule
from app.core.jira_client import jira
from app.services.completion import DONE_MARKS, build_ticket_summary

logger = logging.getLogger(__name__)

# 완료 판정에 쓰는 티켓 종류. 예방4 태그 유무만 다르고 같은 증설 작업이라
# 판정 기준은 동일하게 둔다 (태그 없음은 근거 문구/화면 배지로만 구분).
CAPACITY_KINDS = ("예방4", "증설")

# 변경작업내용(match_text) 안의 마운트/디스크그룹 표기로 DATA(일반)/ARCH(아카이브) 티켓 판별
# 같은 서버가 DATA·ARCH 양쪽 시트에 다 나오는 경우가 많아서, IP/호스트명만으로는 어느 쪽
# 작업인지 구분이 안 됨 -> 변경작업내용 텍스트로 소속 시트를 가려낸다.
# 주의: \bDATA/\bRECO는 뒤쪽 경계가 없어 "DATABASE"/"RECOVERY" 같은 일반 단어의 접두부에도
# 걸린다. 티켓 설명엔 "데이터베이스(Database)"가 거의 항상 들어가므로, 이 lookahead가 없으면
# 실제로는 ARCH(RECO) 작업인 티켓도 DATA로 오판정돼 조용히 걸러져버린다.
_DATA_PATTERNS = [re.compile(r"/oradata", re.I), re.compile(r"\bDATA(?!BASE)", re.I)]
# ARCH 쪽은 오래 비대칭이었다: DATA는 맨 단어 "DATA"도 잡는데 ARCH는 "/arch"(슬래시
# 필수)와 "RECO"만 잡아서, "DATA 영역 2T, Arch 영역 1T 증설"처럼 흔한 표기가 DATA로만
# 분류됐다(바로 위 주석의 예시가 그 표기다). 그러면 한 티켓으로 두 영역을 같이 증설해도
# ARCH 시트에서는 그 대상이 끝내 완료로 안 잡힌다 - 실제로 겪은 문제다.
# 그래서 슬래시 없는 "arch", "archive", 한글 "아카이브"도 같은 영역으로 본다.
# "ARCHIVE"는 \bARCH\b에 안 걸리므로(뒤가 단어문자) 따로 둔다.
_ARCH_PATTERNS = [
    re.compile(r"/arch", re.I),
    re.compile(r"\bRECO(?!VERY)", re.I),
    re.compile(r"\bARCH\b", re.I),
    re.compile(r"\bARCHIVE\b", re.I),
    re.compile(r"아카이브"),
]


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


# 증설을 뜻하는 표현. 태그 없는 티켓에서 '증설 작업이 맞는지'를 보는 데 쓴다
# (디스크 교체/장애 처리 티켓이 같은 서버에 걸려도 들어오지 않게).
_EXPAND_RE = re.compile(r"증설|확장|extend", re.I)


def capacity_ticket_kind(f: dict) -> str:
    """
    티켓 종류 판별.

    - "예방4": 제목에 예방4 태그가 붙은 정식 용량관리 티켓
    - "증설" : 태그는 없지만 디스크 영역 증설로 읽히는 티켓
    - "기타" : 그 밖 (build_capacity_ticket_summary에서 목록에 아예 안 담긴다)

    예방4 태그를 빼고 올리는 경우가 잦아서, 태그가 없어도 증설 티켓이면 같은 종류로
    본다. 판별은 '증설 표현' + '디스크 영역 표기(classify_capacity_sheet)' 두 조건이다.

    영역 판별을 classify_capacity_sheet로 하는 게 중요하다. 처음엔 여기에
    '디스크/파일시스템/볼륨/ASM' 같은 낱말 목록을 따로 뒀는데, 실제 티켓은
    "DATA 영역 500G 증설"처럼 적혀서 그 목록에 하나도 안 걸렸다. 그 결과 실제로 증설이
    끝난 대상이 완료로 안 잡히면서, 승격 때문에 분모만 늘고 완료는 안 늘어나는
    상태가 됐다. 같은 판별을 두 군데서 따로 구현하면 이렇게 어긋난다 - 시트 분류에
    이미 쓰이고 검증된 함수 하나로 모은다.

    읽는 텍스트는 build_ticket_summary의 match_text와 똑같이
    제목+본문+변경작업 대상이다. 그래서 filter_tickets_by_sheet가 남긴 티켓은
    반드시 이 함수에서도 "증설"/"예방4"로 판정된다 (두 판정이 어긋날 수 없다).
    """
    summary = f.get("summary", "") or ""
    if "예방4" in summary:
        return "예방4"
    if not settings.capacity_accept_untagged_jira:
        return "기타"

    extra = "\n".join(str(f.get(k) or "") for k in settings.match_field_list)
    text = f"{summary}\n{f.get('description') or ''}\n{extra}"
    if _EXPAND_RE.search(text) and classify_capacity_sheet(text):
        return "증설"
    return "기타"


def build_capacity_ticket_summary(
    issues: list[dict], field_id: str, include_other: bool = False
) -> list[dict]:
    """
    JIRA 원본 -> 필요 필드만. 용량관리 증설 티켓이 아닌 것(kind "기타")은 여기서 버린다.

    버리는 이유: 조회 JQL을 "예방4"에서 "예방4 또는 증설"로 넓힌 뒤로 메모리/CPU/서버
    증설처럼 무관한 티켓이 결과에 섞여 들어오는데, 뒤쪽 코드 중에는 종류를 안 보고
    "매칭된 티켓이 있다"만 보는 곳이 있다. 특히 미응답 대상의 target 승격
    (capacity_data.get_matched_items)이 그렇다 - 무관한 티켓 하나에 승격이 일어나
    완료율 분모만 늘어난다. 예전엔 조회가 예방4만 받아와서 "목록에 있는 티켓은 전부
    용량관리 티켓"이 자동으로 보장됐는데, 조회를 넓히면서 그 전제가 깨졌다.
    목록을 만드는 이 지점에서 걸러 전제를 되돌린다.

    include_other=True면 "기타"까지 그대로 돌려준다 - 진단 화면(/capacity/trace)이
    "이 티켓이 어느 단계에서 빠졌나"를 보여주려면 걸러진 것까지 봐야 하기 때문이다.
    이 필터 때문에 정작 원인 파악이 안 되는 상황을 만들지 않으려고 둔 예외다.
    """
    tickets = build_ticket_summary(issues, field_id, kind_fn=capacity_ticket_kind)
    # 원본 issue에만 있는 이슈 연결을 티켓 요약에 옮겨 둔다 (아래에서 키로 따라간다)
    for t, issue in zip(tickets, issues):
        t["linked_keys"] = linked_issue_keys(issue["fields"], settings.jira_project)
        t["linked"] = []

    # 연결 티켓 조회를 여기(목록을 만드는 한 지점)에서 한다. 호출부마다 따로 부르게
    # 두면 어떤 화면은 변경이관 티켓의 완료일을 보고 어떤 화면은 못 보는 상태가 되는데,
    # 그렇게 화면마다 기준이 어긋나는 문제를 이미 여러 번 겪었다.
    attach_linked_change_tickets(tickets)

    if include_other:
        return tickets
    return [t for t in tickets if t["kind"] in CAPACITY_KINDS]


def trace_ticket(t: dict, sheet: str, as_of: date, base_year: int) -> tuple[str, str]:
    """
    티켓 하나가 완료 판정까지 가는 길에서 어느 단계에 걸렸는지 (진단 화면용).
    반환: (수준, 설명) - 수준은 ok / wait / drop.

    judge_capacity·filter_tickets_by_sheet·capacity_ticket_kind가 각각 조용히
    걸러내기 때문에, 화면에서는 "티켓이 분명히 있는데 완료가 아니다"만 보이고 왜
    그런지는 코드를 읽어야만 알 수 있었다. 판정 단계를 같은 순서로 다시 짚어
    문장으로 돌려준다.
    """
    if t["kind"] not in CAPACITY_KINDS:
        return "drop", "증설 티켓으로 인식되지 않음 (제목/본문에 증설 표현 + 디스크 영역 표기가 필요)"

    sheets = classify_capacity_sheet(t.get("match_text"))
    if sheet not in sheets:
        found = ", ".join(sorted(sheets)) or "없음"
        return "drop", f"이 시트({sheet}) 영역이 아님 - 변경작업내용 영역 판별: {found}"

    done_on = capacity_ticket_done_date(t)
    if not done_on:
        linked = ", ".join(link["key"] for link in (t.get("linked") or []))
        extra = (
            f" 연결된 변경관리 티켓({linked})에도 완료일이 없음"
            if linked
            else " 연결된 변경관리 티켓도 없음"
        )
        return "drop", f"변경계획완료일이 비어 있음.{extra}"

    src = done_date_source(t)
    basis = f"변경이관 티켓 {src['key']}의 변경계획완료일" if src else "변경계획완료일"

    start, end = half_window(base_year, "H2")
    if not (start <= done_on <= end):
        return "drop", f"{basis} {done_on}이 집계 구간({start} ~ {end}) 밖"
    if done_on > as_of:
        return "wait", f"{basis} {done_on}이 아직 오지 않음 → '미완료'로 표시"
    return "ok", f"완료로 인정 ({done_on}, 근거: {basis})"


def filter_tickets_by_sheet(ticket_map: dict[str, list[dict]], sheet: str) -> dict[str, list[dict]]:
    """IP/호스트명으로 매칭된 티켓 중, 변경작업내용상 이 시트(DATA/ARCH) 소속인 것만 남긴다."""
    filtered = {}
    for no, tickets in ticket_map.items():
        keep = [t for t in tickets if sheet in classify_capacity_sheet(t.get("match_text"))]
        if keep:
            filtered[no] = keep
    return filtered


def build_no_reply_details(items: list[dict], base_year: int) -> list[dict]:
    """
    '미회신'(증설 여부 O/X 미기입) 대상을 상세 목록에 같이 보여주기 위한 변환.
    완료율(분모/분자)에는 안 들어가고 - 여전히 증설 여부 O(target) 대상 기준 - 화면 상세
    목록에서만 나머지 대상들과 나란히 보여준다. status 배지는 무조건 "미응답"으로 표시.
    """
    result = []
    for item in items:
        sched = parse_schedule(item.get("schedule_raw", ""), base_year)
        result.append({
            "item_no": item["item_no"],
            "sheet": item["sheet"],
            "no": item["no"],
            "ci_name": item["ci_name"],
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "owner": item["owner"],
            "jsm_requester": "",
            "center": item["center"],
            "fs_type": item["fs_type"],
            "infra_type": item["infra_type"],
            "total_gb": item["total_gb"],
            "remaining_gb": item["remaining_gb"],
            "usage_pct": item["usage_pct"],
            "required_gb": item["required_gb"],
            "schedule_raw": item.get("schedule_raw", ""),
            "schedule": sched,
            "schedule_disp": f"{sched.month}/{sched.day}" if sched else (item.get("schedule_raw") or ""),
            "planned": False,
            # 미회신 행은 대상 목록에서 다른 미응답과 똑같이 보인다. 증설 티켓이
            # 확인된 건이라도 여기서는 구분하지 않는다 - "티켓이 걸렸다"를 증설했다는
            # 뜻으로 화면에 내보냈다가, 증설한 적 없는 서버가 증설된 것처럼 표시된
            # 적이 있다. 그 정보는 판정 추적 화면(/capacity/trace)에서만 확인한다.
            "jira_key": "",
            "jira_matched": False,
            "jira_untagged": False,
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


def linked_issue_keys(f: dict, project: str) -> list[str]:
    """
    이슈 연결에서 같은 프로젝트의 연결 티켓 키만. (JSM 요청 프로젝트처럼 다른
    프로젝트로 걸린 링크는 변경관리 티켓이 아니므로 뺀다.)
    """
    prefix = f"{project}-"
    keys = []
    for link in f.get("issuelinks") or []:
        for side in ("outwardIssue", "inwardIssue"):
            issue = link.get(side) or {}
            key = issue.get("key") or ""
            if key.startswith(prefix):
                keys.append(key)
    return keys


def attach_linked_change_tickets(tickets: list[dict]) -> list[dict]:
    """
    '변경이관'으로 연결된 변경관리(CM) 티켓의 날짜를 각 티켓에 붙인다 (원본을 수정).

    증설은 요청(SR) 티켓에서 시작해 변경관리 티켓으로 넘겨 진행한다. 우리 대상과
    호스트명/IP로 매칭되는 건 SR 티켓인데(요청 본문에 서버 정보가 적힌다),
    변경계획완료일은 CM 티켓에만 들어간다. 게다가 CM 티켓 제목은
    "[서버][변경관리][이마트] ... DISK 자원조정 작업(09/28)"처럼 '증설'이라는 말이
    없어서 조회(JQL)에도 안 걸린다. 그래서 링크를 따라가 키로 직접 불러온다.

    이걸 안 하면 SR 티켓은 "변경계획완료일 없음"으로 완료 판정에서 빠지고, 증설이
    끝났는데도 화면에는 '미계획'으로 남는다 - 실제로 그런 대상들이 있었다.

    날짜가 이미 있는 티켓은 링크를 보지 않는다 (불필요한 조회를 줄이고, 자기 날짜가
    연결 티켓 날짜로 덮이지 않게).
    """
    need = [t for t in tickets if not t.get("planned_end_date") and t.get("linked_keys")]
    if not need:
        return tickets

    keys = sorted({k for t in need for k in t["linked_keys"]})
    try:
        issues = jira.get_issues_by_keys(keys, fields=[
            "summary",
            "status",
            settings.planned_end_date_field,
            settings.planned_start_date_field,
        ])
    except Exception as e:
        # 연결 티켓을 못 불러와도 나머지 판정은 그대로 돌아간다
        logger.warning(f"변경관리 연결 티켓 조회 실패 (연결 없이 판정): {e}")
        return tickets

    by_key = {
        i["key"]: {
            "key": i["key"],
            "summary": i["fields"].get("summary", ""),
            "status": (i["fields"].get("status") or {}).get("name", ""),
            "planned_end_date": parse_jira_date(i["fields"].get(settings.planned_end_date_field)),
            "planned_start_date": parse_jira_date(
                i["fields"].get(settings.planned_start_date_field)
            ),
        }
        for i in issues
    }
    for t in need:
        t["linked"] = [by_key[k] for k in t["linked_keys"] if k in by_key]
    return tickets


def capacity_ticket_done_date(t: dict) -> date | None:
    """
    완료로 볼 날짜: 변경계획완료일 (증설은 실전환/무중단 구분이 없다).

    요청(SR) 티켓은 이 칸이 비어 있고 실제 작업은 '변경이관'된 변경관리(CM) 티켓에서
    진행된다. 그래서 자기 날짜가 없으면 연결된 CM 티켓의 변경계획완료일을 대신 본다
    (attach_linked_change_tickets 참고).
    """
    if t.get("kind") not in CAPACITY_KINDS:
        return None
    if t.get("planned_end_date"):
        return t["planned_end_date"]
    dates = [
        d for link in (t.get("linked") or [])
        if (d := link.get("planned_end_date"))
    ]
    return max(dates) if dates else None


def done_date_source(t: dict) -> dict | None:
    """완료일을 어느 티켓에서 가져왔는지 (자기 날짜면 None, 연결 티켓이면 그 티켓)"""
    if t.get("planned_end_date"):
        return None
    done_on = capacity_ticket_done_date(t)
    if not done_on:
        return None
    return next(
        (link for link in (t.get("linked") or []) if link.get("planned_end_date") == done_on),
        None,
    )


def is_untagged(t: dict | None) -> bool:
    """예방4 태그 없이 올라온 증설 티켓인지 (화면에서 구분해 보여주기 위한 표시용)"""
    return bool(t) and t.get("kind") != "예방4"


def judge_capacity(
    item: dict, tickets: list[dict] | None, as_of: date, base_year: int
) -> tuple[bool, str, dict | None]:
    """
    완료 판정 (JIRA 증설 티켓 기준).
    1) 엑셀/웹 '증설 완료' 표기
    2) 매칭된 증설 티켓의 변경계획완료일이 하반기 창 안 + 기준일 이전

    2)의 티켓은 [예방4] 태그가 붙은 것과 태그 없이 올라온 것을 같이 본다
    (capacity_ticket_kind 참고). 태그가 없던 건은 근거 문구에 그 사실을 남긴다 -
    숫자만 맞추고 끝내면 아무도 태그 누락을 바로잡지 않게 된다.
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
        notes = []
        if is_untagged(t):
            notes.append("예방4 태그 없음")
        if (src := done_date_source(t)):
            # 어느 티켓의 날짜로 인정했는지 밝혀둔다 - 나중에 숫자를 되짚을 때 필요하다
            notes.append(f"변경이관 티켓 {src['key']}의 완료일")
        note = (" · " + " · ".join(notes)) if notes else ""
        return True, f"JIRA {t['key']} 증설완료 ({capacity_ticket_done_date(t)}){note}", t

    return False, "", None


def calc_capacity_completion(
    items: list[dict],
    ticket_map: dict[str, list[dict]],
    as_of: date,
    base_year: int | None = None,
) -> dict:
    """
    완료율 계산 (증설 여부 O만 분모). ticket_map은 item['no'](시트 내 번호) 기준.

    상태 판정 기준 (3단계):
    - 완료   : judge_capacity 기준 완료 처리됨
    - 미완료 : JIRA 티켓은 실제로 잡혀 있는데(jira_matched) 아직 완료는 아님
    - 미계획 : 아직 JIRA 티켓이 없음 - 엑셀/웹에 "9월중"처럼 대략적인 일정 텍스트만
               적혀 있어도 실제 티켓이 없으면 미계획으로 본다 (일정 텍스트 존재 여부가
               아니라 티켓 존재 여부가 기준).
    """
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
        planned = bool(in_window)  # "미완료" 여부 = 실제 JIRA 티켓 존재 여부

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
            # 예방4 태그 없이 올라온 티켓으로 잡힌 건 - 화면에 표시해서 담당자에게
            # 태그를 붙여달라고 요청할 수 있게 한다 (연결 자체는 정상으로 본다)
            "jira_untagged": is_untagged(display_ticket),
            # 엑셀 '증설 여부' 회신 없이, 증설 완료가 확인돼 대상이 된 건
            "expanded_without_reply": bool(item.get("expanded_without_reply")),
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
