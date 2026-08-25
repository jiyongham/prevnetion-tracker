# app/core/confluence_client.py
"""Confluence Data Center REST API 조회 (PAT/Bearer 인증). EoS 차주 계획서 페이지 파싱용."""
import requests

from app.config import settings


class ConfluenceClient:
    def __init__(self):
        self.base_url = settings.confluence_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {settings.confluence_pat}",
            "Accept": "application/json",
        })

    def get_content(self, page_id: str, expand: str = "") -> dict:
        """페이지 메타/본문 조회. expand 예: 'ancestors', 'body.storage', 'ancestors,body.storage'"""
        params = {"expand": expand} if expand else {}
        resp = self.session.get(f"{self.base_url}/rest/api/content/{page_id}", params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def get_children(self, page_id: str, limit: int = 200) -> list[dict]:
        """하위 페이지 목록"""
        resp = self.session.get(
            f"{self.base_url}/rest/api/content/{page_id}/child/page",
            params={"limit": limit},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def get_siblings(self, page_id: str) -> list[dict]:
        """같은 부모를 가진 형제 페이지 목록 (자기 자신 포함)"""
        page = self.get_content(page_id, expand="ancestors")
        ancestors = page.get("ancestors", [])
        if not ancestors:
            return [page]
        parent_id = ancestors[-1]["id"]
        return self.get_children(parent_id)

    def get_attachments(self, page_id: str, limit: int = 200) -> list[dict]:
        """페이지 첨부파일 목록 (id, title, _links.download 포함)"""
        resp = self.session.get(
            f"{self.base_url}/rest/api/content/{page_id}/child/attachment",
            params={"limit": limit, "expand": "_links"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def download_attachment(self, attachment: dict) -> bytes:
        """attachment(get_attachments 결과 항목)의 실제 파일 바이너리"""
        download_path = attachment["_links"]["download"]
        resp = self.session.get(self.base_url + download_path, timeout=30)
        resp.raise_for_status()
        return resp.content


confluence = ConfluenceClient()
