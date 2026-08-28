# app/services/eos_chatbot.py
"""
EoS 대시보드 진척 조회 챗봇용 컨텍스트 조립.

용량관리(capacity_chatbot)와 같은 방식 - 사내 LLM Agent가 tool calling을 지원하는지
몰라서 Python에서 먼저 관련 대상을 찾아 필드:값 형태로 넣어주고, 에이전트는 그 위에서
자연어로만 답한다. 에이전트가 숫자를 재계산하거나 대상을 지어내지 않게 하려는 것.

EoS는 일정이 '월' 단위라("조치계획 (8월)") "8월 완료대상 뭐야?" 같은 월 기준 질문이
가장 많다. 그래서 질문에서 월을 먼저 뽑아 그 달 대상을 통째로 컨텍스트에 넣는다
- 이름/시스템명 매칭만으로는 월 질문에 아무것도 못 붙여준다.
"""
import logging
import re
from datetime import date

from app.config import settings
from app.core.agent_client import agent_chat, extract_answer
from app.services.eos import build_no_reply_details, calc_eos_completion, filter_track
from app.services.eos_data import get_eos_data
from app.services.eos_report import DB_TOTAL_FIXED, OS_TOTAL_FIXED
from app.services.reminder import clean_name, parse_owners

logger = logging.getLogger(__name__)

MAX_ITEMS_IN_CONTEXT = 40
MAX_MONTH_ITEMS = 60      # 월 질의는 한 달에 수십 대가 잡혀 별도 상한
MIN_HOSTNAME_LEN = 4

# "8월", "'26년 8월", "2026년 8월", "2026-08"
_MONTH_RE = re.compile(r"(?:(\d{2,4})\s*년\s*)?(\d{1,2})\s*월")
_YM_RE = re.compile(r"(\d{4})[-/.](\d{1,2})")
_RELATIVE = {
    "이번달": 0, "이번 달": 0, "금월": 0, "당월": 0,
    "다음달": 1, "다음 달": 1, "차월": 1, "내달": 1,
    "지난달": -1, "지난 달": -1, "전월": -1,
}


def _shift_month(base: date, delta: int) -> tuple[int, int]:
    m = base.month - 1 + delta
    return base.year + m // 12, m % 12 + 1


def parse_month(query: str, today: date | None = None) -> tuple[int, int] | None:
    """질문에서 대상 연월을 뽑는다. 없으면 None (월 기준 질문이 아님)."""
    today = today or date.today()

    for word, delta in _RELATIVE.items():
        if word in query:
            return _shift_month(today, delta)

    m = _YM_RE.search(query)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))

    m = _MONTH_RE.search(query)
    if not m:
        return None
    year_part, month_part = m.groups()
    month = int(month_part)
    if not 1 <= month <= 12:
        return None
    year = today.year
    if year_part:
        y = int(year_part)
        year = y if y > 100 else 2000 + y
    return year, month


def parse_track(query: str) -> str:
    """질문에 OS/DB 트랙이 명시돼 있으면 그 트랙으로 좁힌다."""
    upper = query.upper()
    has_os = "OS" in upper
    has_db = "DB" in upper or "데이터베이스" in query
    if has_os and not has_db:
        return "OS"
    if has_db and not has_os:
        return "DB"
    return "ALL"


# ─────────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────────
def get_status_rows(as_of: date) -> list[dict]:
    """
    EoS 전체 대상을 진척 조회용으로 모은다 (target/no_reply/excluded 3분류).
    대시보드 라우트(routes/eos.py)가 화면에 조립하는 것과 같은 기준을 그대로 쓴다
    - 따로 두면 대시보드와 챗봇 답이 어긋난다.
    """
    items, ticket_map, polestar_confirmed, _ = get_eos_data()
    result = calc_eos_completion(items, ticket_map, as_of, polestar_confirmed=polestar_confirmed)

    # OS/DB 트랙은 계산 결과(details)에 안 실려 있어 원본 항목에서 따로 끌어온다
    track_of = {
        i["item_no"]: ("OS/DB" if i.get("os_eos_target") and i.get("db_eos_target")
                       else "OS" if i.get("os_eos_target")
                       else "DB" if i.get("db_eos_target") else "")
        for i in items
    }

    rows = [{**d, "status_kind": "target", "track": track_of.get(d["item_no"], "")} for d in result["details"]]
    rows += [
        {**d, "status_kind": "no_reply", "track": track_of.get(d["item_no"], "")}
        for d in build_no_reply_details([i for i in items if i["status"] == "no_reply"], as_of.year)
    ]
    for i in items:
        if i["status"] == "excluded":
            rows.append({
                **i,
                "status_kind": "excluded",
                "track": track_of.get(i["item_no"], ""),
                "completed": False,
                "planned": False,
                "schedule": None,
                "schedule_disp": i.get("schedule_raw") or "",
                "jira_key": "",
                "reason": f"제외 (사유: {i.get('exclude_reason') or '미기재'})",
            })
    return rows


MAX_OWNERS_SHOWN = 3


def _fmt_owner(raw: str) -> str:
    """담당자가 팀 전원(8명)으로 들어있는 대상이 많아, 컨텍스트가 터지지 않게 줄인다."""
    names = [n.strip() for n in (raw or "").split("||") if n.strip()]
    if len(names) <= MAX_OWNERS_SHOWN:
        return ", ".join(names)
    return ", ".join(names[:MAX_OWNERS_SHOWN]) + f" 외 {len(names) - MAX_OWNERS_SHOWN}명"


# ─────────────────────────────────────────────
# 컨텍스트 조립
# ─────────────────────────────────────────────
def _fmt_row(d: dict) -> str:
    return (
        f"- 시스템명: {d.get('system_name', '')} / 호스트명: {d.get('hostname', '')} / "
        f"IP: {d.get('ip', '')} / 트랙: {d.get('track') or '미상'} / "
        f"운영팀: {d.get('ops_team', '')} / 담당자: {_fmt_owner(d.get('owner', ''))} / "
        f"status_kind: {d.get('status_kind', '')} / completed: {d.get('completed')} / "
        f"planned: {d.get('planned')} / 조치계획: {d.get('schedule_disp') or '없음'} / "
        f"jira_key: {d.get('jira_key') or '없음'} / reason: {d.get('reason') or '없음'}"
    )


def _overall_block(as_of: date) -> str:
    """트랙별 전체 진척. 모수는 착수 시점 고정값(OS 384 / DB 49)을 리포트와 동일하게 쓴다."""
    items, ticket_map, polestar_confirmed, _ = get_eos_data()
    lines = []
    for track, fixed in (("OS", OS_TOTAL_FIXED), ("DB", DB_TOTAL_FIXED)):
        r = calc_eos_completion(
            filter_track(items, track), ticket_map, as_of, polestar_confirmed=polestar_confirmed
        )
        rate = round(r["done"] / fixed * 100, 1) if fixed else 0.0
        lines.append(
            f"- {track}: 전체 {fixed}대 / 진행대상 {r['total']}대 / 완료 {r['done']}대 / "
            f"진척률 {rate}% / 미계획 {r['no_schedule']}대"
        )
    return "[전체 진척 (기준일 " + as_of.isoformat() + ")]\n" + "\n".join(lines)


def _month_block(rows: list[dict], year: int, month: int, track: str) -> str:
    scoped = [r for r in rows if track == "ALL" or track in (r.get("track") or "").split("/")]
    hit = [
        r for r in scoped
        if r.get("schedule") and r["schedule"].year == year and r["schedule"].month == month
    ]
    done = sum(1 for r in hit if r.get("completed"))
    head = (
        f"[{year}년 {month}월 조치계획 대상"
        + (f" - {track} 트랙" if track != "ALL" else "")
        + f"] 총 {len(hit)}대 / 완료 {done}대 / 미완료 {len(hit) - done}대"
    )
    if not hit:
        return head + "\n(해당 월에 조치계획이 잡힌 대상 없음)"
    listed = sorted(hit, key=lambda r: (not r.get("completed"), r.get("system_name") or ""))
    body = "\n".join(_fmt_row(r) for r in listed[:MAX_MONTH_ITEMS])
    if len(hit) > MAX_MONTH_ITEMS:
        body += f"\n... 외 {len(hit) - MAX_MONTH_ITEMS}대 (목록 일부만 표시)"
    return head + "\n" + body


def _find_by_system(rows: list[dict], query: str) -> list[dict]:
    q = query.lower()
    return [
        r for r in rows
        if (r.get("system_name") and r["system_name"].lower() in q)
        or (r.get("hostname") and len(r["hostname"]) >= MIN_HOSTNAME_LEN and r["hostname"].lower() in q)
        or (r.get("ip") and r["ip"] in query)
    ]


def _find_by_name(rows: list[dict], name: str) -> list[dict]:
    target = clean_name(name)
    return [
        r for r in rows
        if any(clean_name(o["name"]) == target for o in parse_owners(r.get("owner", "")))
    ]


def build_context(name: str, query: str, as_of: date | None = None) -> str:
    as_of = as_of or date.today()
    rows = get_status_rows(as_of)

    blocks = [_overall_block(as_of)]

    ym = parse_month(query)
    track = parse_track(query)
    if ym:
        blocks.append(_month_block(rows, ym[0], ym[1], track))

    # 월 질의가 아닐 때만 개인/시스템 매칭을 붙인다 (월 목록과 섞이면 컨텍스트가 너무 커짐)
    if not ym:
        picked, seen = [], set()
        for d in (_find_by_name(rows, name) + _find_by_system(rows, query)):
            key = d.get("item_no")
            if key in seen:
                continue
            seen.add(key)
            picked.append(d)
            if len(picked) >= MAX_ITEMS_IN_CONTEXT:
                break
        if picked:
            blocks.append(f"[{name}님 관련 / 질문에 언급된 EoS 대상]\n" + "\n".join(_fmt_row(d) for d in picked))
        else:
            blocks.append(f"[{name}님 명의로 매칭된 EoS 대상 없음 - 전체 진척 기준으로만 답변 가능]")

    return "\n\n".join(blocks)


def answer(name: str, query: str) -> str:
    full_query = f"{build_context(name, query)}\n\n[사용자 질문]\n{query}"
    result = agent_chat(
        user_id=name or "eos-status-chat",
        query=full_query,
        agent_id=settings.eos_status_agent_id,
        agent_code=settings.eos_status_agent_code,
    )
    return extract_answer(result)
