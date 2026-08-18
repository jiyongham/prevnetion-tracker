import json

import requests
from app.config import settings


def _post_card(webhook: str, text: str) -> tuple[bool, str]:
    """Teams Incoming Webhook으로 AdaptiveCard 텍스트 메시지 발송 (공통)"""
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
        resp = requests.post(webhook, json=payload, timeout=30)
        if resp.status_code in (200, 202):
            return True, ""
        return False, f"{resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def _post_text(webhook: str, text: str) -> tuple[bool, str]:
    """
    DM 트리거용 메시지 발송.
    Adaptive Card(attachments)만 보내면 Teams 채널 메시지 본문(body/content)에는
    실제 텍스트가 안 들어가고 <attachment> 참조 태그만 남아, Flow의
    "새 채널 메시지" 트리거가 빈 값을 받는다. 그래서 top-level 'text' 필드에
    실제 내용을 실어 보낸다 (Bot Framework Activity 스키마 호환).
    """
    payload = {
        "type": "message",
        "text": text,
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
        resp = requests.post(webhook, json=payload, timeout=30)
        if resp.status_code in (200, 202):
            return True, ""
        return False, f"{resp.status_code} {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def send_teams_message(text: str) -> bool:
    """Teams 워크플로우 Webhook으로 메시지 발송 (주간 리포트 등, 채널 공개용)"""
    if not settings.teams_webhook:
        print("⚠️ TEAMS_WEBHOOK 미설정 - 발송 스킵 (콘솔 출력만)")
        return False
    ok, err = _post_card(settings.teams_webhook, text)
    print("✅ Teams 발송 완료" if ok else f"❌ Teams 발송 실패: {err}")
    return ok


def send_teams_dm(name: str, team: str, message: str) -> tuple[bool, str]:
    """
    개인 1:1 DM 발송 트리거.
    HTTP 프리미엄 커넥터 없이 처리하기 위해, 전용(비공개) 채널의 웹훅으로
    '마커+JSON' 메시지를 올린다. Power Automate가 그 채널을
    "새 채널 메시지가 추가되면"(비프리미엄) 트리거로 감지 → 파싱 →
    Office 365 Users 검색(이름+팀) → 해당자에게 개인 DM.
    """
    if not settings.teams_dm_trigger_webhook:
        return False, "TEAMS_DM_TRIGGER_WEBHOOK 미설정 (.env 확인)"

    payload_json = json.dumps(
        {"name": name, "team": team, "message": message}, ensure_ascii=False
    )
    text = f"{settings.dm_marker}{payload_json}"
    return _post_text(settings.teams_dm_trigger_webhook, text)
