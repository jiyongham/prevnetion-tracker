# app/services/evidence_check.py
"""
증적란에 적힌 JIRA 티켓의 실제 상태 점검.

담당자가 증적으로 티켓 번호를 적어두지만, 그 티켓이 실제로 종결됐는지는 아무도 다시
확인하지 않는다. 실제로 상반기 대사 중에 '반려'된 티켓이 13개 대상의 증적으로 인용돼
있었고, '등록' 상태로 방치된 티켓이 6개 대상에 걸려 반기가 끝나 있었다.

여기서 하는 일은 조회와 표시뿐이다 - 증적이 부실하다고 완료 판정을 뒤집지는 않는다.
그건 담당자가 확인하고 판단할 일이고, 코드가 조용히 완료를 취소하면 더 큰 혼란이 된다.
"""
import logging
import re
import time

from app.core.jira_client import jira

logger = logging.getLogger(__name__)

# 증적란에 섞여 들어오는 자유 텍스트에서 티켓 키만 추출 (예: "IMDC-12345 참조", "메일 전달")
_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,}-\d{2,})\b")

# 종결로 볼 상태. 이 밖의 상태(등록/진행중/반려/중단 등)면 증적으로 불충분하다고 본다.
CLOSED_STATUSES = {"완료", "종료", "해결됨", "Closed", "Done", "Resolved"}
# 명백히 증적이 될 수 없는 상태 (경고 수준을 높여 표시)
REJECTED_STATUSES = {"반려", "중단", "취소", "Rejected", "Cancelled"}

CACHE_TTL_SEC = 600
_cache: dict = {"at": 0.0, "value": {}}

CHUNK = 40


def extract_keys(evidence: str) -> list[str]:
    """증적 문자열에서 JIRA 티켓 키 목록 (없으면 빈 리스트)"""
    return _KEY_RE.findall(evidence or "")


def _fetch_statuses(keys: list[str]) -> dict[str, str]:
    """
    키 목록 -> {키: 상태명}. 존재하지 않는 키가 하나라도 섞이면 JQL 전체가 400으로
    떨어지므로, 청크 단위로 조회하고 실패한 청크만 한 건씩 다시 시도한다.
    """
    result: dict[str, str] = {}
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i:i + CHUNK]
        try:
            issues = jira.search(f"key in ({','.join(chunk)})", fields=["status"])
        except Exception:
            issues = []
            for k in chunk:
                try:
                    issues += jira.search(f"key = {k}", fields=["status"])
                except Exception:
                    logger.info(f"증적 티켓 조회 불가 (없는 키이거나 권한 없음): {k}")
        for iss in issues:
            result[iss["key"]] = iss["fields"]["status"]["name"]
    return result


def get_statuses(keys: list[str], force_refresh: bool = False) -> dict[str, str]:
    """캐시된 {키: 상태명}. 아직 조회한 적 없는 키만 추가로 불러온다."""
    now = time.time()
    if force_refresh or now - _cache["at"] > CACHE_TTL_SEC:
        _cache["value"] = {}
        _cache["at"] = now

    missing = sorted({k for k in keys if k not in _cache["value"]})
    if missing:
        _cache["value"].update(_fetch_statuses(missing))
        # 조회했는데 안 나온 키는 '없는 티켓'으로 기록해 매번 다시 묻지 않는다
        for k in missing:
            _cache["value"].setdefault(k, "")
    return {k: _cache["value"].get(k, "") for k in keys}


def judge(evidence: str, statuses: dict[str, str]) -> tuple[str, str]:
    """
    증적 상태 판정. 반환: (수준, 설명)
      ""        - 티켓 키가 없거나(자유 텍스트 증적) 전부 종결됨
      "warn"    - 미종결(등록/진행중 등) 티켓이 섞여 있음
      "bad"     - 반려/중단된 티켓이거나 존재하지 않는 티켓
    """
    keys = extract_keys(evidence)
    if not keys:
        return "", ""

    bad, warn = [], []
    for k in keys:
        st = statuses.get(k, "")
        if not st:
            bad.append(f"{k}(조회 안 됨)")
        elif st in REJECTED_STATUSES:
            bad.append(f"{k}({st})")
        elif st not in CLOSED_STATUSES:
            warn.append(f"{k}({st})")

    if bad:
        return "bad", "증적 티켓 확인 필요: " + ", ".join(bad + warn)
    if warn:
        return "warn", "증적 티켓 미종결: " + ", ".join(warn)
    return "", ""


def annotate(details: list[dict]) -> list[dict]:
    """
    각 행에 evidence_level / evidence_note를 붙인다 (원본 dict를 그대로 수정).
    JIRA 조회가 실패해도 화면은 떠야 하므로 예외는 삼키고 표시만 생략한다.
    """
    keys = sorted({k for d in details for k in extract_keys(d.get("evidence", ""))})
    if not keys:
        return details
    try:
        statuses = get_statuses(keys)
    except Exception as e:
        logger.warning(f"증적 티켓 상태 조회 실패 (표시 생략): {e}")
        return details

    for d in details:
        d["evidence_level"], d["evidence_note"] = judge(d.get("evidence", ""), statuses)
    return details
