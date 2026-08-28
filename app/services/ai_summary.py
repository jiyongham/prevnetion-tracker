# app/services/ai_summary.py
"""
주간 리포트 '이번 주 특이사항' 한 줄 생성 (DR훈련/용량관리 공용).

원래 report.py 안에 DR훈련 전용으로 있던 걸 도메인만 파라미터로 받게 빼냈다 -
프롬프트의 핵심 제약(과거 데이터가 없으니 추세 언급 금지, 지금 스냅샷 사실만)은
도메인과 무관하게 똑같이 필요해서 하나의 에이전트를 공유한다.
"""
from app.config import settings
from app.core.agent_client import agent_chat, extract_answer


def generate_weekly_summary(
    domain_label: str,
    result: dict,
    by_team: dict,
    unit: str = "건",
) -> str | None:
    """
    현재 스냅샷(전체/팀별 완료율)만 근거로 특이사항 한 줄 생성.
    에이전트 미설정/실패 시 None (리포트 발송 자체는 막지 않는다).

    domain_label: "DR 모의훈련 하반기 진척 현황"처럼 프롬프트에 그대로 들어갈 도메인 설명
    unit: 집계 단위 ("건" / "대")
    """
    if not settings.summary_agent_id:
        return None

    team_lines = "\n".join(
        f"- {team}: {v['done']}/{v['total']}{unit} ({v['rate']}%)"
        for team, v in sorted(by_team.items(), key=lambda x: x[1]["rate"])
    )
    query = (
        f"아래는 {domain_label} 스냅샷입니다. 이 데이터만 근거로, "
        "이번 리포트에서 눈에 띄는 특이사항을 한 문장으로 짚어주세요 "
        "(예: 진척이 유독 느린 팀, 미계획 비중이 큰 점 등). "
        "과거 데이터가 없으니 추세(예: '몇 주째')는 절대 언급하지 말고, "
        "지금 데이터에 있는 사실만 쓰세요.\n\n"
        f"전체: {result['done']}/{result['total']}{unit} ({result['rate']}%), "
        f"미계획 {result['no_schedule']}{unit}\n"
        f"팀별 현황(낮은 순):\n{team_lines}"
    )
    try:
        r = agent_chat(
            user_id="system-report",
            query=query,
            agent_id=settings.summary_agent_id,
            agent_code=settings.summary_agent_code,
        )
        return extract_answer(r)
    except Exception as e:
        print(f"⚠️ 주간 특이사항 생성 실패 (리포트는 정상 발송): {e}")
        return None
