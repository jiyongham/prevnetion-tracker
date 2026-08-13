# app/core/jira_client.py
import requests
from requests.auth import HTTPBasicAuth
from app.config import settings


class JiraClient:
    def __init__(self):
        self.base_url = settings.jira_url.rstrip("/")
        self.auth = HTTPBasicAuth(settings.jira_user, settings.jira_password)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Accept": "application/json"})

    def search(self, jql: str, fields: list[str] | None = None, max_results: int = 100):
        """JQL로 이슈 검색"""
        url = f"{self.base_url}/rest/api/2/search"
        params = {"jql": jql, "maxResults": max_results}
        if fields:
            params["fields"] = ",".join(fields)

        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("issues", [])

    def get_issue(self, issue_key: str):
        """단일 이슈 조회 (필드 구조 확인용)"""
        url = f"{self.base_url}/rest/api/2/issue/{issue_key}"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()


jira = JiraClient()
