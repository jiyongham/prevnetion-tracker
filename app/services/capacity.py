# app/services/capacity.py
import re
from datetime import date

from app.config import settings
from app.core.capacity_loader import get_targets
from app.core.date_utils import half_window, parse_schedule
from app.services.completion import DONE_MARKS, build_ticket_summary

# 완료 판정에 쓰는 티켓 종류. 예방4 태그 유무만 다르고 같은 증설 작업이라
# 판정 기준은 동일하게 둔다 (태그 없음은 근거 문구/화면 배지로만 구분).
CAPACITY_KINDS = ("예방4", "증설")

# 태그 없는 티켓을 증설로 인정할 때 쓰는 표현들.
# '증설' 한 단어만 보면 메모리/CPU/서버 증설까지 걸리므로, 증설을 뜻하는 말과
# 디스크 영역을 가리키는 말이 함께 있어야 용량관리 티켓으로 본다.
# 디스크 쪽 표현은 일부러 좁게 잡았다 - '용량', '마운트'처럼 변경 티켓 양식에
# 흔히 들어가는 낱말을 넣으면 사실상 아무 티켓이나 통과해버린다.
_EXPAND_RE = re.compile(r"증설|확장|extend", re.I)
_DISK_RE = re.compile(
    r"디스크|파일\s*시스템|filesystem|file\s*system|볼륨|volume"
    r"|\bASM\b|디스크\s*그룹|diskgroup|/oradata|/arch|\bLVM\b",
    re.I,
)


def capacity_ticket_kind(f: dict) -> str:
    """
    티켓 종류 판별.

    - "예방4": 제목에 예방4 태그가 붙은 정식 용량관리 티켓
    - "증설" : 태그는 없지만 디스크/파일시스템/ASM 증설로 읽히는 티켓
    - "기타" : 그 밖 (완료 판정에 쓰이지 않는다)

    예방4 태그를 빼고 올리는 경우가 잦아서, 태그가 없어도 증설 티켓이면 같은 종류로
    본다. 제목만 보면 '[영향도협의] OO서버 증설'처럼 어느 영역인지 안 드러나는 경우가
    많아 본문과 변경작업 대상까지 같이 읽는다 (매칭에 쓰는 텍스트와 같은 범위 -
    completion.build_ticket_summary의 match_text 참고).

    이 함수가 마지막 방어선은 아니다. 여기를 통과한 티켓도
    match_items_by_ip(대상과 IP/호스트명 일치) -> filter_tickets_by_sheet(DATA/ARCH
    영역 확인)를 다시 지나야 대상에 연결된다.
    """
    summary = f.get("summary", "") or ""
    if "예방4" in summary:
        return "예방4"
    if not settings.capacity_accept_untagged_jira:
        return "기타"

    extra = "\n".join(str(f.get(k) or "") for k in settings.match_field_list)
    text = f"{summary}\n{f.get('description') or ''}\n{extra}"
    if _EXPAND_RE.search(text) and _DISK_RE.search(text):
        return "증설"
    return "기타"


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


def capacity_ticket_done_date(t: dict) -> date | None:
    """완료로 볼 날짜: 변경계획완료일 (증설은 실전환/무중단 구분이 없다)"""
    if t.get("kind") in CAPACITY_KINDS:
        return t.get("planned_end_date")
    return None


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
        note = " · 예방4 태그 없음" if is_untagged(t) else ""
        return True, f"JIRA {t['key']} 증설완료 ({t['planned_end_date']}){note}", t

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
