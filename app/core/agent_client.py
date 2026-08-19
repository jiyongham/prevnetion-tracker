# app/core/agent_client.py
"""사내 LLM Agent 게이트웨이 연동. client_credentials 토큰을 캐싱해서 재사용한다."""
import time

import requests

from app.config import settings

_token_cache = {"access_token": None, "expires_at": 0.0}


def _fetch_token() -> dict:
    resp = requests.post(
        settings.agent_token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.agent_client_id,
            "client_secret": settings.agent_client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_access_token() -> str:
    """만료 60초 전이면 미리 갱신해서 캐싱된 토큰을 재사용"""
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    data = _fetch_token()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 300)
    return _token_cache["access_token"]


def agent_chat(user_id: str, query: str) -> dict:
    """
    Agent Chat 호출. 401(토큰 만료)이면 한 번 재발급 후 재시도한다.
    """
    payload = {
        "user": user_id,
        "query": query,
        "response_mode": "blocking",
        "agent_id": settings.agent_id,
        "agent_code": settings.agent_code,
    }

    def _call(token: str):
        resp = requests.post(
            f"{settings.agent_gateway_url}/api/v1/agent/chat",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        return resp

    resp = _call(get_access_token())
    if resp.status_code == 401:
        _token_cache["access_token"] = None
        resp = _call(get_access_token())
    resp.raise_for_status()
    return resp.json()
