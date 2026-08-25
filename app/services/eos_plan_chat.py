# app/services/eos_plan_chat.py
"""
EoS 차주 계획을 JIRA/Confluence로 자동 취합하기 어려운 주에, 관리자가 아는 대로
자유 텍스트로 말하면("다음주에 까사미아 SAP, 이마트 인시생산성시스템 예정이야")
사내 LLM Agent가 대상 시스템 후보를 뽑아 확인 목록으로 보여주는 챗봇 기능.

에이전트에게 목록을 자유 생성하게 하지 않고, 우리 EoS 대상 목록을 번호 붙여 주고
"이 번호 중에서만 고르라"고 시킨다 - 존재하지 않는 시스템을 지어내는 걸 막기 위함.
최종 확정은 사람이 체크박스로 확인 후 저장.
"""
import re

from app.config import settings
from app.core.agent_client import agent_chat, extract_answer

_NUM_RE = re.compile(r"\d+")


def build_candidates(items: list[dict]) -> list[dict]:
    """EoS 대상(target) 항목만 후보로 - [{no, item_no, label}, ...]"""
    targets = [i for i in items if i.get("is_target")]
    return [
        {
            "no": idx + 1,
            "item_no": t["item_no"],
            "label": t.get("system_name") or t["item_no"],
            "ops_team": t.get("ops_team", ""),
        }
        for idx, t in enumerate(targets)
    ]


def parse_plan_message(message: str, candidates: list[dict]) -> list[dict]:
    """자유 텍스트에서 언급된 것으로 보이는 후보들을 반환 (없으면 빈 리스트)."""
    if not message.strip() or not candidates:
        return []

    listing = "\n".join(f"{c['no']}. {c['label']}" for c in candidates)
    query = (
        "당신은 EoS(노후 서버 전환) 프로젝트의 작업 계획을 파악하는 도우미입니다.\n"
        "아래는 이 프로젝트의 전체 대상 시스템 목록입니다 (번호. 시스템명):\n"
        f"{listing}\n\n"
        f'담당자가 다음과 같이 말했습니다: "{message}"\n\n'
        "이 발언에서 언급된 것으로 보이는 시스템의 번호만 답하세요. "
        "목록에 없는 시스템을 지어내지 마세요. 확실하지 않으면 포함하지 마세요.\n"
        '답변 형식: 번호만 쉼표로 구분해서 (예: 1,5,12). 언급된 게 하나도 없으면 정확히 "없음"이라고만 답하세요.'
    )
    result = agent_chat(
        user_id="system-eos-plan-chat",
        query=query,
        agent_id=settings.eos_plan_agent_id,
        agent_code=settings.eos_plan_agent_code,
    )
    answer = extract_answer(result)

    nums = {int(n) for n in _NUM_RE.findall(answer)}
    if not nums:
        return []

    by_no = {c["no"]: c for c in candidates}
    return [by_no[n] for n in sorted(nums) if n in by_no]
