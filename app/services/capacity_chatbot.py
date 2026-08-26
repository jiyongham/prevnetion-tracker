# app/services/capacity_chatbot.py
"""
용량관리 챗봇 (현황 조회 / 서버별 기준 계산 - 두 모드).
DR훈련 chatbot.py와 같은 방식: Python이 먼저 관련 데이터를 찾아 컨텍스트로 넣어주고
에이전트는 자연어 답변만 만든다 (실제 함수 호출 없음 - agent_client.py 참고).

두 모드 다 같은 컨텍스트 빌더를 쓰지만(서버별 기준 계산도 결국 그 서버의 실제 수치가
필요하므로), agent_id/agent_code만 모드에 따라 다른 에이전트로 갈린다.
"""
from app.config import settings
from app.core.agent_client import agent_chat, extract_answer
from app.services.reminder import clean_name, parse_owners

MAX_ITEMS_IN_CONTEXT = 30
MIN_HOSTNAME_LEN = 4


def _status_label(d: dict) -> str:
    """
    상태 5분류. details는 세 군데에서 올 수 있어(calc_capacity_completion의 대상 상세,
    build_no_reply_details의 미응답 상세, load_capacity_items_merged의 제외 원본) 필드
    구성이 조금씩 다른데, 이 함수 하나로 다 걸러낸다.
    """
    if d.get("status_kind") == "excluded" or d.get("is_excluded"):
        return "제외"
    if d.get("no_reply") or d.get("status_kind") == "no_reply":
        return "미응답"
    if d.get("completed"):
        return "완료"
    if not d.get("planned"):
        return "미계획"
    return "미완료"


def _fmt_item(d: dict) -> str:
    status = _status_label(d)
    sched = d.get("schedule_disp") or d.get("schedule_raw") or "없음"
    jira = f" (JIRA {d['jira_key']})" if d.get("jira_key") else ""
    infra = d.get("infra_type") or "-"
    usage = f"{d['usage_pct']}%" if d.get("usage_pct") is not None else "-"
    total = d.get("total_gb")
    required = d.get("required_gb")
    return (
        f"- {d.get('ci_name', '')} / {d.get('hostname', '')} / {d.get('ip', '')} / "
        f"시트 {d.get('sheet', '')} / 인프라 {infra} / 사용률 {usage} / "
        f"전체 {total if total is not None else '-'}GB / 필요 {required if required is not None else '-'}GB / "
        f"일정 {sched} / 상태 {status}{jira}"
    )


def _my_items(details: list[dict], name: str) -> list[dict]:
    target = clean_name(name)
    return [
        d for d in details
        if any(clean_name(o["name"]) == target for o in parse_owners(d.get("owner", "")))
    ]


def _matched_by_query(details: list[dict], query: str) -> list[dict]:
    q = query.lower()
    return [
        d for d in details
        if (d.get("ci_name") and d["ci_name"].lower() in q)
        or (d.get("hostname") and len(d["hostname"]) >= MIN_HOSTNAME_LEN and d["hostname"].lower() in q)
        or (d.get("ip") and d["ip"] in query)
    ]


def build_context(name: str, query: str, details: list[dict]) -> str:
    mine = _my_items(details, name)
    matched = _matched_by_query(details, query)

    seen: set[str] = set()
    rows = []
    for d in (mine + matched)[:MAX_ITEMS_IN_CONTEXT]:
        key = d.get("item_no") or f"{d.get('sheet')}:{d.get('no')}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(_fmt_item(d))

    if not rows:
        return f"[{name}님 명의로 매칭된 용량관리 대상 없음]"
    return f"[{name}님 관련 용량관리 대상 데이터]\n" + "\n".join(rows)


def answer(name: str, query: str, details: list[dict], mode: str = "status") -> str:
    """mode: 'status'(현황 조회) | 'criteria'(서버별 기준 계산)"""
    context = build_context(name, query, details)
    full_query = f"{context}\n\n[사용자 질문]\n{query}"

    if mode == "criteria":
        agent_id = settings.capacity_criteria_agent_id or None
        agent_code = settings.capacity_criteria_agent_code or None
    else:
        agent_id = settings.capacity_status_agent_id or None
        agent_code = settings.capacity_status_agent_code or None

    result = agent_chat(user_id=name, query=full_query, agent_id=agent_id, agent_code=agent_code)
    return extract_answer(result)
