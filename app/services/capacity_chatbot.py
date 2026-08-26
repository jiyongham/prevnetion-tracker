# app/services/capacity_chatbot.py
"""
용량관리 대시보드의 두 챗봇 에이전트용 컨텍스트 조립.

역할이 명확히 나뉜 두 에이전트를 쓴다 (시스템 프롬프트가 서로 배타적이라 하나로
합치면 안 됨):
- 산정 기준 설명(capacity_calc_agent)  : 이 서버가 왜/어떻게 증설 대상인지 계산식 설명
- 진척 조회(capacity_status_agent)     : 완료/일정/JIRA 등 현황 조회 (기존 dr.py의
  chatbot.py와 같은 패턴 - DR훈련 전용 기본 에이전트로는 용량관리를 못 다뤄 별도 에이전트 필요)

사내 LLM Agent가 tool calling을 지원하는지 몰라서(chatbot.py와 동일한 이유),
Python에서 먼저 관련 서버를 찾아 필드값을 텍스트로 넣어주고 에이전트는 그 위에서
자연어로만 답하게 한다. 에이전트 시스템 프롬프트가 "반환된 필드 그대로 전달하고
재해석하지 말라"고 못박혀 있어, 컨텍스트도 가공된 요약 문장이 아니라 필드:값 형태로 준다.
"""
import logging
from datetime import date

from app.config import settings
from app.core.agent_client import agent_chat, extract_answer
from app.core.capacity_loader import load_capacity_items_merged
from app.core.jira_client import jira
from app.services.capacity import (
    build_capacity_ticket_summary,
    build_no_reply_details,
    calc_capacity_completion,
    filter_tickets_by_sheet,
)
from app.services.matcher import match_items_by_ip
from app.services.reminder import clean_name, parse_owners

logger = logging.getLogger(__name__)

SHEETS = ("DATA", "ARCH")
MAX_ITEMS_IN_CONTEXT = 20
MIN_HOSTNAME_LEN = 4


def _find_by_system(rows: list[dict], query: str) -> list[dict]:
    q = query.lower()
    return [
        r for r in rows
        if (r.get("ci_name") and r["ci_name"].lower() in q)
        or (r.get("hostname") and len(r["hostname"]) >= MIN_HOSTNAME_LEN and r["hostname"].lower() in q)
        or (r.get("ip") and r["ip"] in query)
    ]


def _find_by_name(rows: list[dict], name: str) -> list[dict]:
    target = clean_name(name)
    return [
        r for r in rows
        if any(clean_name(o["name"]) == target for o in parse_owners(r.get("owner", "")))
    ]


# ─────────────────────────────────────────────
# 산정 기준 설명 에이전트
# ─────────────────────────────────────────────
def _fmt_calc_row(d: dict) -> str:
    return (
        f"- CI명: {d.get('ci_name', '')} / 호스트명: {d.get('hostname', '')} / 시트: {d.get('sheet', '')} / "
        f"infra_type: {d.get('infra_type') or '확인 안 됨'} / total_gb: {d.get('total_gb')} / "
        f"usage_pct: {d.get('usage_pct')} / remaining_gb: {d.get('remaining_gb')} / "
        f"required_gb: {d.get('required_gb')}"
    )


def answer_calc(query: str) -> str:
    """
    산정 기준 설명 - 완료/일정/JIRA 조회 없이 엑셀의 용량 수치만 필요해서 JIRA 호출이 없다.
    """
    rows = []
    for sheet in SHEETS:
        rows += load_capacity_items_merged(sheet=sheet)

    matched = _find_by_system(rows, query)[:MAX_ITEMS_IN_CONTEXT]
    if matched:
        context = "[해당 서버 데이터]\n" + "\n".join(_fmt_calc_row(d) for d in matched)
    else:
        context = "[질의에서 특정 서버(CI명/호스트명)를 찾지 못함 - 일반 계산식 질문이면 서버 데이터 없이도 답변 가능]"

    full_query = f"{context}\n\n[사용자 질문]\n{query}"
    result = agent_chat(
        user_id="capacity-calc-chat",
        query=full_query,
        agent_id=settings.capacity_calc_agent_id,
        agent_code=settings.capacity_calc_agent_code,
    )
    return extract_answer(result)


# ─────────────────────────────────────────────
# 진척 조회 에이전트
# ─────────────────────────────────────────────
def _fmt_status_row(d: dict) -> str:
    return (
        f"- CI명: {d.get('ci_name', '')} / 호스트명: {d.get('hostname', '')} / IP: {d.get('ip', '')} / "
        f"시트: {d.get('sheet', '')} / status_kind: {d.get('status_kind', '')} / "
        f"completed: {d.get('completed')} / planned: {d.get('planned')} / "
        f"schedule_disp: {d.get('schedule_disp') or '없음'} / jira_key: {d.get('jira_key') or '없음'} / "
        f"reason: {d.get('reason') or '없음'}"
    )


def _get_status_rows(sheet: str) -> list[dict]:
    """
    이 시트의 전체 대상을 진척 조회용으로 한데 모은다 (target/no_reply/excluded 통일).
    대시보드 라우트(routes/capacity.py)가 화면에 보여주려고 조립하는 것과 같은 3분류를
    챗봇 컨텍스트용으로도 그대로 쓴다 - 로직을 따로 두면 대시보드와 챗봇 답이 어긋난다.
    """
    items = load_capacity_items_merged(sheet=sheet)

    ticket_map = {}
    try:
        issues = jira.get_capacity_tickets()
        tickets = build_capacity_ticket_summary(issues, settings.planned_end_date_field)
        targets = [i for i in items if i["is_target"]]
        matched = match_items_by_ip(targets, tickets)["matched"]
        ticket_map = filter_tickets_by_sheet(matched, sheet)
    except Exception as e:
        logger.warning(f"용량관리 JIRA 조회 실패 (엑셀 기준으로 계속): {e}")

    result = calc_capacity_completion(items, ticket_map, date.today())
    rows = [{**d, "status_kind": "target"} for d in result["details"]]

    no_reply_raw = [i for i in items if i["status_kind"] == "no_reply"]
    rows += [
        {**d, "status_kind": "no_reply"}
        for d in build_no_reply_details(no_reply_raw, date.today().year)
    ]

    for i in items:
        if i["status_kind"] == "excluded":
            rows.append({
                **i,
                "status_kind": "excluded",
                "completed": False,
                "planned": False,
                "jira_key": "",
                "schedule_disp": i.get("schedule_raw") or "",
                "reason": f"제외 (사유: {i.get('exclude_reason') or '미기재'})",
            })
    return rows


def _get_all_status_rows() -> list[dict]:
    rows = []
    for sheet in SHEETS:
        try:
            rows += _get_status_rows(sheet)
        except Exception as e:
            logger.warning(f"용량관리 {sheet} 시트 조회 실패: {e}")
    return rows


def answer_status(name: str, query: str) -> str:
    rows = _get_all_status_rows()
    mine = _find_by_name(rows, name)
    matched = _find_by_system(rows, query)

    seen: set[tuple] = set()
    picked = []
    for d in (mine + matched)[:MAX_ITEMS_IN_CONTEXT]:
        key = (d.get("sheet"), d.get("item_no") or d.get("no"))
        if key in seen:
            continue
        seen.add(key)
        picked.append(d)

    if picked:
        context = f"[{name}님 관련 용량관리 대상 데이터]\n" + "\n".join(_fmt_status_row(d) for d in picked)
    else:
        context = f"[{name}님 명의로 매칭된 용량관리 대상 없음]"

    full_query = f"{context}\n\n[사용자 질문]\n{query}"
    result = agent_chat(
        user_id=name,
        query=full_query,
        agent_id=settings.capacity_status_agent_id,
        agent_code=settings.capacity_status_agent_code,
    )
    return extract_answer(result)
