# app/core/jira_client.py
import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from app.config import settings


class JiraClient:
    def __init__(self):
        self.base_url = settings.jira_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(settings.jira_user, settings.jira_password)
        self.session.headers.update({"Accept": "application/json"})
        # CMDB(Insight) 병렬 조회(ThreadPoolExecutor)가 동시에 여러 연결을 쓰므로 풀을 넉넉히 잡는다
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

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
        """
        DR 티켓 조회 (IP 매칭용 - 본문 포함)
        - 실전환: 제목에 "예방3"
        - 무중단: 제목에 "무중단" (예방3 없음)
        - 작업 구분(customfield_19529)에 "DR훈련"이 체크된 경우도 포함 (전환기: 제목 태그만으론
          안 걸리는 티켓도 놓치지 않기 위해 OR로 추가. 예: 제목에 예방3/무중단이 없어도 이 필드로 걸림)
        """
        jql = (
            f'project = {settings.jira_project} '
            f'AND (summary ~ "예방3" OR summary ~ "무중단" OR "작업 구분" = "DR훈련") '
            f'ORDER BY created DESC'
        )
        fields = [
            "summary",
            "description",
            "status",
            "created",
            settings.jsm_requester_field,
            settings.planned_end_date_field,
            settings.planned_start_date_field,
            settings.dr_work_type_field,
            *settings.match_field_list,   # 변경작업 대상 등 (호스트명/IP 포함)
        ]
        return self.search(jql, fields=fields)

    def get_capacity_tickets(self):
        """용량관리(ASM/파일시스템 증설) 티켓 조회 - 제목에 "예방4" """
        jql = (
            f'project = {settings.jira_project} '
            f'AND summary ~ "예방4" '
            f'ORDER BY created DESC'
        )
        fields = [
            "summary",
            "description",
            "status",
            "created",
            settings.jsm_requester_field,
            settings.planned_end_date_field,
            settings.planned_start_date_field,
            *settings.match_field_list,
        ]
        return self.search(jql, fields=fields)

    def get_eos_tickets(self):
        """EoS(노후 OS/DB 전환) 티켓 조회 - 제목에 "예방1" """
        jql = (
            f'project = {settings.jira_project} '
            f'AND summary ~ "예방1" '
            f'ORDER BY created DESC'
        )
        fields = [
            "summary",
            "description",
            "status",
            "created",
            settings.jsm_requester_field,
            settings.planned_end_date_field,
            settings.planned_start_date_field,
            *settings.match_field_list,
        ]
        return self.search(jql, fields=fields)


jira = JiraClient()
