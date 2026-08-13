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
