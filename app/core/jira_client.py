# app/core/jira_client.py
import requests
from requests.auth import HTTPBasicAuth
from app.config import settings


class JiraClient:
    def __init__(self):
        self.base_url = settings.jira_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(settings.jira_user, settings.jira_password)
        self.session.headers.update({"Accept": "application/json"})

    def search(self, jql: str, fields: list[str] | None = None, max_results: int = 500):
        """JQL 검색 (페이징 처리)"""
        url = f"{self.base_url}/rest/api/2/search"
        all_issues = []
        start_at = 0

        while True:
            params = {"jql": jql, "startAt": start_at, "maxResults": 100}
            if fields:
                params["fields"] = ",".join(fields)

            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            issues = data.get("issues", [])
            all_issues.extend(issues)

            start_at += len(issues)
            if start_at >= data.get("total", 0) or not issues or start_at >= max_results:
                break

        return all_issues

    def get_dr_tickets(self):
        """[예방3] DR 티켓 조회 (IP 매칭용 - 본문 포함)"""
        jql = (
            f'project = {settings.jira_project} '
            f'AND summary ~ "예방3" '
            f'ORDER BY created DESC'
        )
        fields = [
            "summary",
            "description",
            "status",
            settings.planned_end_date_field,
        ]
        return self.search(jql, fields=fields)


jira = JiraClient()
