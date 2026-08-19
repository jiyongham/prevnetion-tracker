# app/services/ai_diagnose.py
"""
LLM 진단 도우미 (사용자가 버튼을 눌렀을 때만 호출 — 크레딧 절약을 위해 자동/일괄 실행 없음).
1) JIRA 매칭이 안 된 대상: 왜 안 됐는지, 비슷한 후보 티켓이 있는지 진단.
2) 담당자 불일치 후보: 오탈자/표기 누락인지, 실제 조직개편으로 보이는지 판단.
"""
import re
from datetime import date

from app.config import settings
from app.core.agent_client import agent_chat, extract_answer
from app.core.date_utils import half_window


def _diagnose_chat(query: str) -> str:
    result = agent_chat(
        user_id="system-diagnose",
        query=query,
        agent_id=settings.diagnose_agent_id,
        agent_code=settings.diagnose_agent_code,
    )
    return extract_answer(result)

CANDIDATE_LIMIT = 5
MIN_TOKEN_LEN = 3


def find_candidate_tickets(item: dict, tickets: list[dict]) -> list[dict]:
    """
    호스트명 토큰이 하나라도 겹치는 티켓들 (오탈자/부분일치 후보) 중,
    변경계획시작일이 올해(현재 연도) 하반기 안에 있는 것만. 겹치는 토큰 수 순.
    """
    start, end = half_window(date.today().year, "H2")
    in_h2 = [
        t for t in tickets
        if (d := t.get("planned_start_date")) and start <= d <= end
    ]

    host = (item.get("hostname") or "").lower()
    tokens = [p for p in re.split(r"[-_.]", host) if len(p) >= MIN_TOKEN_LEN]
    if not tokens:
        return []

    scored = []
    for t in in_h2:
        text = (t.get("match_text") or "").lower()
        hits = sum(1 for tok in tokens if tok in text)
        if hits:
            scored.append((hits, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:CANDIDATE_LIMIT]]


def diagnose_unmatched(item: dict, candidates: list[dict]) -> str:
    target_line = f"대상: {item['system_name']} / 호스트명 {item['hostname']} / IP {item['ip']}"

    if not candidates:
        context = f"{target_line}\n이 호스트명과 조금이라도 겹치는 JIRA 티켓이 전혀 없습니다."
    else:
        lines = [f"- {c['key']}: {c['summary']}" for c in candidates]
        context = (
            f"{target_line}\n"
            "이 대상과 정확히 매칭되는 JIRA 티켓은 없지만, 아래처럼 호스트명 일부가 겹치는 "
            "티켓들이 있습니다:\n" + "\n".join(lines)
        )

    query = (
        "당신은 DR훈련 진척 관리 시스템의 매칭 진단 도우미입니다. "
        "아래 대상이 왜 JIRA 티켓과 자동 매칭이 안 됐는지 후보 티켓을 보고 1~2문장으로 진단해주세요. "
        "가능성 있는 원인(호스트명 오탈자, 아직 티켓 미생성, 완전히 무관한 티켓들 등)을 짧게 짚고, "
        "후보 중 실제로 이 대상일 가능성이 있는 티켓이 있으면 티켓 키를 언급해주세요. "
        "근거 없이 단정짓지 말고, 모르면 모른다고 답하세요.\n\n" + context
    )
    return _diagnose_chat(query)


def diagnose_mismatch(row: dict) -> str:
    query = (
        "당신은 담당자 정보 정확도를 점검하는 도우미입니다. 아래는 한 시스템의 담당자 관련 데이터입니다:\n"
        f"- 엑셀 등록 담당자: {row.get('current_owner') or '(없음)'}\n"
        f"- CMDB 시스템운영팀: {row.get('cmdb_ops_team') or '(없음)'}\n"
        f"- CMDB 시스템담당자: {row.get('cmdb_owners') or '(없음)'}\n"
        f"- 최근 JIRA 티켓 JSM요청자: {row.get('jsm_requester_name') or '(없음)'} "
        f"({row.get('jsm_requester_team') or '(없음)'})\n\n"
        "이 차이의 성격을 판단해서, 반드시 아래 형식 그대로 답하세요 (형식을 벗어나면 안 됩니다):\n"
        "1번째 줄: [오탈자] 또는 [조직변경] 또는 [불명확] 중 하나만 (엑셀 쪽 표기 실수/미업데이트로 "
        "보이면 [오탈자], 실제 담당 조직이 바뀐 것으로 보이면 [조직변경], 판단 근거가 부족하면 [불명확])\n"
        "2번째 줄: 확신 정도: 높음 / 보통 / 낮음 중 하나\n"
        "3번째 줄부터: 판단 이유를 1문장으로"
    )
    return _diagnose_chat(query)
