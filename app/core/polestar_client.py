# app/core/polestar_client.py
"""
Polestar(NKIA EMS 8) REST API 조회. EoS 실제 전환 완료 판정용.

작업이 정상 완료되면 CI명에서 TO-BE의 '_NEW'가 빠지고 AS-IS에 '_OLD'가 붙는데,
CMDB(JIRA Insight)는 작업자가 늦게 반영하는 경우가 있어 Polestar를 기준으로 삼는다.

※ 리소스 조회 API는 현재 인증 없이 응답한다(사내망 기준). login()은 인터페이스
  정의서 규격대로 구현해두되, 조회에 실패할 때만 쓰도록 선택적으로 남겨둔다.
※ 리소스 등록 시 '설명'란에 넣는 호스트명/벤더/모델은 REST API로 제공되지 않는다
  (문서화된 엔드포인트/응답 필드에 없음). 그래서 호스트명 대신 IP를 조인 키로 쓴다.
"""
import requests

from app.config import settings


class PolestarClient:
    def __init__(self):
        self.base_url = settings.polestar_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def login(self) -> bool:
        """인터페이스 정의서 규격의 로그인. 조회 API가 열려 있어 평소엔 호출하지 않는다."""
        if not settings.polestar_user:
            return False
        resp = self.session.post(
            f"{self.base_url}/rest/login",
            json={"name": settings.polestar_user, "password": settings.polestar_password},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json().get("data", {}).get("result", {})
        return bool(result.get("success"))

    def list_resources(self, resource_type: str = "all") -> list[dict]:
        """
        전체 리소스(CI) 목록. 항목 예:
        {"name": "[아,Nu] 클라우드 POS AD #1_OLD", "ipAddress": "10.0.0.1",
         "id": 1885231313, "resourceStatus": "UNMANAGED", "availability": "DISABLED",
         "resourceType": "server.Server", "parentId": ...}
        """
        resp = self.session.get(
            f"{self.base_url}/rest/resource/list",
            params={"type": resource_type},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("list", [])

    def get_resource(self, resource_id) -> dict:
        """리소스 상세 (list 필드 + osType/host/upTime)"""
        resp = self.session.get(f"{self.base_url}/rest/resource/{resource_id}", timeout=20)
        resp.raise_for_status()
        return resp.json().get("configuration", {})

    def get_children(self, resource_id, recursive: bool = True) -> list[dict]:
        """
        하위 리소스. recursive=True면 손자까지(allChildren).
        서버 아래에 server.FileSystem, oracle.ASMDiskGroup, oracle.Tablespace 등이 달린다.
        """
        path = "allChildren" if recursive else "children"
        resp = self.session.get(f"{self.base_url}/rest/resource/{resource_id}/{path}", timeout=30)
        resp.raise_for_status()
        return resp.json().get("configuration", [])

    def get_definition_names(self, resource_type: str) -> list[str]:
        """해당 리소스 타입에서 수집 가능한 지표명 (예: oracle.ASMDiskGroup -> free_size/total_size/utilization)"""
        resp = self.session.get(f"{self.base_url}/rest/measure/definitionName/{resource_type}", timeout=20)
        resp.raise_for_status()
        return resp.json().get("measurement", [])

    def get_measures(self, resource_id, definitions: list[str]) -> dict[str, float]:
        """
        리소스의 최근 수집값. 반환은 {지표명(한글): 값}.
        수집이 안 되는 리소스는 빈 dict (모니터링 미구성이거나 에이전트 중단).
        """
        resp = self.session.get(
            f"{self.base_url}/rest/measure/custom",
            params={"resourceId": resource_id, "definition": ",".join(definitions)},
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", {}).get("measurement", [])
        return {r["definitionName"]: r["value"] for r in rows if "definitionName" in r}

    def search_by_ip(self, ips: list[str]) -> list[dict]:
        """IP로 리소스 검색 (여러 개면 콤마 구분)"""
        if not ips:
            return []
        resp = self.session.get(
            f"{self.base_url}/rest/resource/list/search",
            params={"ip": ",".join(ips)},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("list", [])


polestar = PolestarClient()
