# app/core/jira_client.py
import concurrent.futures
import logging

import requests
from requests.adapters import HTTPAdapter
from app.config import settings

logger = logging.getLogger(__name__)

# 한 번에 받아올 이슈 수. JIRA Server 기본 상한(jira.search.views.default.max)이
# 보통 100이라 더 키워도 서버가 조용히 잘라서 준다.
PAGE_SIZE = 100
# 페이지 병렬 조회 수. 세션 커넥션 풀(pool_maxsize=20)과 JIRA 부하를 함께 고려한 값.
MAX_PAGE_WORKERS = 6


class JiraClient:
    def __init__(self):
        self.base_url = settings.jira_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {settings.jira_pat}",
            "Accept": "application/json",
        })
        # CMDB(Insight) 병렬 조회(ThreadPoolExecutor)가 동시에 여러 연결을 쓰므로 풀을 넉넉히 잡는다
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _search_page(self, url: str, jql: str, fields_param: str | None, start_at: int, page_size: int) -> dict:
        """검색 한 페이지"""
        params = {"jql": jql, "startAt": start_at, "maxResults": page_size}
        if fields_param:
            params["fields"] = fields_param
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search(self, jql: str, fields: list[str] | None = None):
        """
        JQL 검색 (전체 건수를 다 가져올 때까지 페이징).
        예전엔 max_results=500 상한이 있었는데, ORDER BY created DESC라
        상한을 넘는 티켓 중 "생성일이 오래된" 쪽이 통째로 잘려나갔다. EoS(예방1)가
        864건까지 쌓이면서 실제로 이 상한에 걸려, 예정된 작업인데도 티켓이 오래전에
        만들어졌다는 이유만으로 리포트/대시보드에서 누락되는 문제가 있었다.

        첫 페이지를 받아 total을 안 뒤 나머지 페이지는 병렬로 가져온다. 순차로 돌리면
        EoS(1,100건 이상 = 12페이지)에서 페이지당 1초씩 10초 넘게 걸렸는데, 페이지끼리
        의존이 없어 동시에 부를 수 있다.

        페이징 중에 티켓이 생기거나 사라지면 offset이 밀려 같은 이슈가 두 페이지에
        걸릴 수 있으므로 key 기준으로 중복을 제거한다.
        """
        url = f"{self.base_url}/rest/api/2/search"
        fields_param = ",".join(fields) if fields else None

        first = self._search_page(url, jql, fields_param, 0, PAGE_SIZE)
        issues = first.get("issues", [])
        total = first.get("total", 0)
        if not issues or len(issues) >= total:
            return issues

        # 서버가 요청보다 작은 페이지를 줄 수 있어(설정 상한) 실제 응답 크기를 보폭으로 쓴다
        step = len(issues)
        offsets = list(range(step, total, step))
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PAGE_WORKERS) as ex:
            pages = list(ex.map(
                lambda start: self._search_page(url, jql, fields_param, start, step),
                offsets,
            ))

        by_key = {i["key"]: i for i in issues}
        for page in pages:
            for issue in page.get("issues", []):
                by_key.setdefault(issue["key"], issue)

        if len(by_key) < total:
            logger.info(f"JIRA 검색 {len(by_key)}/{total}건 (조회 중 티켓 변동 가능): {jql[:80]}")
        return list(by_key.values())

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
        """
        EoS(노후 OS/DB 전환) 티켓 조회.
        제목에 "예방1"이 없어도 IP전환 작업이면 대상에 포함 - 실제 EoS 대상 여부는
        이후 매칭 단계(호스트명/IP/CMDB Key)에서 우리 대상 목록과 겹치는지로 걸러진다.
        [예방1] 태그만 고집하면, 같은 작업이 다른 이유(예: 타 팀 작업에 묶여)로
        예방1 태그 없이 등록된 경우를 놓친다.
        """
        jql = (
            f'project = {settings.jira_project} '
            f'AND (summary ~ "예방1" OR summary ~ "IP전환" OR summary ~ "IP 전환") '
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
            settings.eos_cmdb_done_field,
            *settings.match_field_list,
        ]
        return self.search(jql, fields=fields)


jira = JiraClient()
