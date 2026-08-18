import requests
from app.config import settings


def send_teams_message(text: str) -> bool:
    """Teams 워크플로우 Webhook으로 메시지 발송"""
    if not settings.teams_webhook:
        print("⚠️ TEAMS_WEBHOOK 미설정 - 발송 스킵 (콘솔 출력만)")
        return False

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{
                    "type": "TextBlock",
                    "text": text,
                    "wrap": True,
                }],
            },
        }],
    }

    try:
        resp = requests.post(settings.teams_webhook, json=payload, timeout=30)
        if resp.status_code in (200, 202):
            print("✅ Teams 발송 완료")
            return True
        print(f"❌ Teams 발송 실패: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"❌ Teams 발송 오류: {e}")
        return False


def send_teams_dm(name: str, team: str, message: str) -> tuple[bool, str]:
    """
    개인 1:1 DM 발송 (Power Automate 흐름 호출).
    앱은 이름/팀/메시지만 넘기고, 이메일 조회·발송은 Flow가 담당한다.
    Flow HTTP 트리거는 {"name","team","message"} JSON을 받도록 구성.
    """
    if not settings.teams_flow_url:
        return False, "TEAMS_FLOW_URL 미설정 (.env 확인)"

    payload = {"name": name, "team": team, "message": message}
    try:
        resp = requests.post(settings.teams_flow_url, json=payload, timeout=30)
        if resp.status_code in (200, 202):
            return True, ""
        return False, f"{resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return False, str(e)
