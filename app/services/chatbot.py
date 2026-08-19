# app/services/chatbot.py
"""
담당자 조회 챗봇. 사내 LLM Agent가 tool calling을 지원하는지 아직 몰라서,
Python에서 먼저 관련 데이터를 찾아 컨텍스트로 넣어주고 에이전트는 자연어 답변만
만들게 하는 방식으로 구성한다 (플랫폼 종류와 무관하게 동작).
"""
from app.core.agent_client import agent_chat
from app.services.reminder import clean_name, parse_owners

MAX_ITEMS_IN_CONTEXT = 30
MIN_HOSTNAME_LEN = 4


def _fmt_item(d: dict) -> str:
    status = "완료" if d["completed"] else ("미계획" if not d["planned"] else "미완료")
    jira = f" (JIRA {d['jira_key']})" if d.get("jira_key") else ""
    return (
        f"- {d['system_name']} / {d['hostname']} / {d['ip']} / "
        f"일정 {d['schedule_disp'] or '없음'} / {status}{jira}"
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
        if (d.get("system_name") and d["system_name"].lower() in q)
        or (d.get("hostname") and len(d["hostname"]) >= MIN_HOSTNAME_LEN and d["hostname"].lower() in q)
        or (d.get("ip") and d["ip"] in query)
    ]


def build_context(name: str, query: str, details: list[dict]) -> str:
    mine = _my_items(details, name)
    matched = _matched_by_query(details, query)

    seen: set[str] = set()
    rows = []
    for d in (mine + matched)[:MAX_ITEMS_IN_CONTEXT]:
        if d["no"] in seen:
            continue
        seen.add(d["no"])
        rows.append(_fmt_item(d))

    if not rows:
        return f"[{name}님 명의로 매칭된 DR훈련 대상 없음]"
    return f"[{name}님 관련 DR훈련 대상 데이터]\n" + "\n".join(rows)


def _extract_answer(result: dict) -> str:
    """에이전트 응답 스키마가 아직 불확실해서, 흔한 키 후보를 순서대로 시도"""
    if isinstance(result, str):
        return result
    for path in (
        ("answer",), ("data", "answer"),
        ("result",), ("message",), ("output",), ("data", "output"),
    ):
        node = result
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node:
            return node
    return str(result)


def answer(name: str, query: str, details: list[dict]) -> str:
    context = build_context(name, query, details)
    full_query = f"{context}\n\n[사용자 질문]\n{query}"
    result = agent_chat(user_id=name, query=full_query)
    return _extract_answer(result)
