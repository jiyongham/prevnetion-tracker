# app/services/last_report.py
"""
방금 발송한 리포트 본문을 잠깐 기억해 둔다 (화면 확인용).

수동 발송(진척률 발송)은 Teams로 나가버려서, 누른 사람은 무엇이 나갔는지 Teams를
열어보기 전엔 알 수 없었다. 발송 직후 대시보드에 같은 본문을 띄워주기 위한 임시 보관소다.

리다이렉트 URL에 본문을 실어 보내지 않는 이유: 인코딩하면 2KB가 넘어 주소창이 지저분해지고,
새로고침/북마크로 옛 리포트가 계속 되살아난다. 여기 두면 URL엔 sent=1만 붙는다.

프로세스 메모리라 재시작하면 사라지고, 워커가 여러 개면 다른 워커에는 없다.
못 찾으면 그냥 안 보여주면 되는 부가 정보라 그 이상 붙들지 않는다 (원본은 Teams에 남는다).
"""
import time

TTL_SEC = 300

_last: dict[str, tuple[float, str]] = {}


def remember(domain: str, text: str) -> None:
    """발송 성공 직후 본문 보관 (domain: 'dr' | 'capacity' | 'eos')"""
    _last[domain] = (time.time(), text)


def get(domain: str) -> str:
    """보관된 본문. 없거나 TTL이 지났으면 빈 문자열"""
    entry = _last.get(domain)
    if not entry or time.time() - entry[0] >= TTL_SEC:
        return ""
    return entry[1]
