import html
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


def to_dm_html(text: str) -> str:
    """
    개인 DM 본문용 HTML 변환.

    Flow의 'Post message in a chat or channel'이 올리는 Teams 메시지 본문은 HTML로
    렌더돼서, 평문의 줄바꿈('\n')과 탭이 화면에서는 공백 하나로 접혀 사라진다.
    그래서 줄바꿈은 <br>, 들여쓰기 탭은 고정폭 공백으로 바꾼 사본을 message_html로
    같이 실어 보내고, Flow는 이 필드를 메시지 본문에 넣는다.
    (평문 message도 그대로 남겨둔다 - 예전 Flow 및 로그/디버깅 호환)

    시스템명에 '<', '&'가 들어가도 태그로 해석되지 않도록 먼저 이스케이프한다.
    작은따옴표까지 이스케이프하면 본문에 &#x27;이 보이므로 quote=False로 둔다.
    """
    escaped = html.escape(text, quote=False)
    return escaped.replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;").replace("\n", "<br>")


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
        {
            "name": name,
            "team": team,
            "message": message,
            "message_html": to_dm_html(message),
        },
        ensure_ascii=False,
    )
    text = f"{settings.dm_marker}{payload_json}"
    return _post_text(settings.teams_dm_trigger_webhook, text)
