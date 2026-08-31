# app/services/report_check.py
"""
리포트 발송 전 이상 감지.

주간 리포트는 자동 발송이라 아무도 안 보고 나가는데, 데이터 소스(JIRA/Confluence/
Polestar/엑셀)가 하나라도 조용히 실패하면 "완료 0대" 같은 숫자가 그대로 경영 보고
채널에 올라간다. 그렇다고 숫자만 보고는 그게 정상(아직 그 주가 안 옴)인지 장애인지
구분이 안 된다 - 지난 발송분과 비교해야 알 수 있다.

설계 원칙은 리포트 본문과 같다: 이상 '감지'는 규칙(코드)이 하고, 그 이상이 데이터
문제인지 정상 변동인지 '판단'만 에이전트가 한다. 숫자 판정을 LLM에 맡기면 오탐/누락이
생기고, 애초에 규칙으로 정확히 잡을 수 있는 일이다.

에이전트 미설정/실패 시에도 규칙 기반 경고는 그대로 나간다.
"""
import logging

from app.config import settings
from app.core.agent_client import agent_chat, extract_answer
from app.models.db import get_last_report_snapshot, save_report_snapshot

logger = logging.getLogger(__name__)

DOMAIN_LABELS = {"dr": "DR 모의훈련", "capacity": "용량관리(디스크 증설)"}

# 진행률이 이 이상 급변하면 데이터 이상을 의심 (정상 주간 변동폭을 크게 넘는 값)
RATE_JUMP_THRESHOLD = 20.0


def detect_anomalies(metrics: dict, prev: dict | None) -> list[str]:
    """
    지난 발송분 대비 이상 징후를 규칙으로 찾아낸다. 첫 발송(prev=None)이면 비교 대상이
    없으므로 '값이 통째로 0인 경우'만 본다.
    """
    issues = []
    total = metrics.get("total") or 0
    done = metrics.get("done") or 0

    if total == 0:
        issues.append("전체 대상이 0대입니다 (엑셀 로드 실패 의심).")

    if prev is None:
        if total > 0 and done == 0:
            issues.append("완료 대수가 0대입니다 (첫 발송이라 지난주 비교 불가).")
        return issues

    prev_done = prev.get("done") or 0
    prev_total = prev.get("total") or 0
    prev_rate = prev.get("rate") or 0.0
    rate = metrics.get("rate") or 0.0

    # 완료는 누적이라 줄어들 수 없다 - 줄었다면 매칭/판정 로직이나 데이터 소스 문제
    if done < prev_done:
        issues.append(f"완료 대수가 지난주 {prev_done}대에서 {done}대로 줄었습니다.")

    if prev_total and total != prev_total:
        issues.append(f"전체 대상이 지난주 {prev_total}대에서 {total}대로 바뀌었습니다.")

    if abs(rate - prev_rate) >= RATE_JUMP_THRESHOLD:
        issues.append(f"진행률이 지난주 {prev_rate}%에서 {rate}%로 급변했습니다.")

    # 지난주엔 잡히던 주간 집계가 이번에 0이면 소스 실패 가능성
    for key, label in (("perf_cnt", "금주 실적"), ("plan_cnt", "차주 계획")):
        cur, before = metrics.get(key), prev.get(key)
        if cur == 0 and (before or 0) > 0:
            issues.append(f"{label}이 지난주 {before}대에서 0대가 됐습니다.")

    issues += detect_composition_drift(metrics.get("composition"), prev.get("composition"))
    return issues


def detect_composition_drift(cur: dict | None, prev: dict | None) -> list[str]:
    """
    대상 '구성'이 바뀐 걸 잡아낸다. 총계 비교만으로는 실전환 6대가 무중단으로 재분류되는
    식의 변경(합계는 그대로)을 놓치는데, 그게 원본 엑셀이 조용히 수정됐다는 신호다.
    구성은 {"그룹명": {"항목": 대수}} 형태 (예: {"수행방식": {"실전환": 139, "무중단": 33}}).
    """
    if not cur or not prev:
        return []

    issues = []
    for group, cur_counts in cur.items():
        prev_counts = prev.get(group)
        if not isinstance(cur_counts, dict) or not isinstance(prev_counts, dict):
            continue
        moves = []
        for key in sorted(set(cur_counts) | set(prev_counts)):
            before, after = prev_counts.get(key, 0), cur_counts.get(key, 0)
            if before != after:
                moves.append(f"{key} {before}→{after}")
        if moves:
            issues.append(f"{group} 구성이 바뀌었습니다 ({', '.join(moves)}) - 원본 엑셀 수정 여부 확인 필요.")
    return issues


def _judge(domain: str, metrics: dict, prev: dict | None, issues: list[str]) -> str | None:
    """감지된 이상을 에이전트가 해석 (미설정/실패 시 None - 규칙 경고만 나간다)"""
    if not settings.report_check_agent_id:
        return None

    prev_line = (
        f"지난주: 전체 {prev.get('total')}대 / 완료 {prev.get('done')}대 / "
        f"진행률 {prev.get('rate')}% / 금주실적 {prev.get('perf_cnt')} / 차주계획 {prev.get('plan_cnt')}"
        if prev else "지난주: 비교 데이터 없음(첫 발송)"
    )
    query = (
        f"당신은 {DOMAIN_LABELS.get(domain, domain)} 주간 리포트를 발송 전 점검하는 도우미입니다.\n"
        "아래는 이번 주 집계와 지난주 집계, 그리고 규칙으로 감지된 이상 징후입니다.\n"
        "이게 (가) 데이터 수집 실패로 의심되는지, (나) 정상적인 변동인지 판단하고 "
        "2문장 이내로 답하세요. 확실하지 않으면 단정하지 말고 확인이 필요하다고 하세요.\n\n"
        f"이번주: 전체 {metrics.get('total')}대 / 완료 {metrics.get('done')}대 / "
        f"진행률 {metrics.get('rate')}% / 금주실적 {metrics.get('perf_cnt')} / 차주계획 {metrics.get('plan_cnt')}\n"
        f"{prev_line}\n"
        "감지된 이상:\n" + "\n".join(f"- {i}" for i in issues)
    )
    try:
        r = agent_chat(
            user_id="system-report-check",
            query=query,
            agent_id=settings.report_check_agent_id,
            agent_code=settings.report_check_agent_code,
        )
        return extract_answer(r)
    except Exception as e:
        logger.warning(f"리포트 이상 판단 실패 (규칙 경고만 사용): {e}")
        return None


def check_report(domain: str, metrics: dict) -> str | None:
    """
    발송 전 점검. 이상이 없으면 None, 있으면 경고 문자열.
    스냅샷 저장은 하지 않는다 (발송이 실제로 성공한 뒤에 record_sent로 저장).
    """
    prev = get_last_report_snapshot(domain)
    issues = detect_anomalies(metrics, prev)
    if not issues:
        return None

    parts = ["⚠️ 리포트 이상 징후:"] + [f"  - {i}" for i in issues]
    judgement = _judge(domain, metrics, prev, issues)
    if judgement:
        parts += ["", f"  🤖 {judgement}"]
    return "\n".join(parts)


def record_sent(domain: str, metrics: dict):
    """발송 성공 후 이번 집계를 스냅샷으로 남긴다 (다음 주 비교 기준)"""
    save_report_snapshot(domain, metrics)
