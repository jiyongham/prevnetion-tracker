"""
스크린샷용 CMDB(Insight) 목업.

담당자 확인 화면(owner_check)은 엑셀 담당자와 CMDB 등록 담당자를 대조해서
불일치 후보를 보여준다. 더미 엑셀의 호스트명(host-a00 등)은 실제 CMDB에
존재하지 않으므로, 실제 API를 그대로 두면 조회 결과가 항상 비어 화면에
아무 후보도 안 뜬다 (버그가 아니라 더미 데이터가 실재하지 않기 때문).

이 모듈은 app.core.insight_client.get_server_assets 를 몽키패치해서,
호스트명 문자열 기반으로 결정적인 가짜 자산을 만들어 돌려준다. 엑셀 쪽
담당자 배정 규칙(dummy_data.py, row_idx 기준)과 CMDB 쪽 배정 규칙(호스트명
해시 기준)이 서로 다르므로 자연스럽게 일부는 일치, 일부는 불일치로 갈린다.

사용은 run_dummy_server.py 를 통해서만 한다 (이 모듈을 직접 실행하지 않음).
"""

from app.core import insight_client

DUMMY_TEAMS = ["운영1팀", "운영2팀", "인프라팀", "보안팀", "네트워크팀"]
DUMMY_NAMES = [
    "홍길동", "김영희", "이순신", "박상민", "최지민",
    "이지은", "정민준", "우진서", "조예은", "강현준",
]


def _fake_asset(host: str) -> dict:
    idx = sum(ord(c) for c in host)
    team = DUMMY_TEAMS[idx % len(DUMMY_TEAMS)]
    name = DUMMY_NAMES[idx % len(DUMMY_NAMES)]
    return {
        "object_key": f"ASSET-{10000 + idx % 90000}",
        "status": "운영",
        "ip": f"10.20.{idx % 256}.{(idx * 7) % 256}",
        "ops_team": team,
        "owners": [
            {"raw": f"{name}-{team}", "source": "시스템담당자", "name": name, "team": team}
        ],
        "duplicate_count": 1,
    }


def _fake_get_server_assets(hostnames: list[str]) -> dict[str, dict]:
    results = {}
    for h in hostnames:
        host = (h or "").strip().lower()
        if host:
            results[host] = _fake_asset(host)
    return results


def _fake_get_server_asset(hostname: str) -> dict | None:
    host = (hostname or "").strip().lower()
    return _fake_asset(host) if host else None


def patch_insight_client() -> None:
    """app.core.insight_client 의 CMDB 조회 함수를 더미로 교체한다."""
    insight_client.get_server_assets = _fake_get_server_assets
    insight_client.get_server_asset = _fake_get_server_asset

    # owner_check.py 등에서 `from app.core.insight_client import get_server_assets`
    # 로 이미 이름을 가져간 모듈들도 같이 바꿔줘야 몽키패치가 실제로 먹는다.
    import app.services.owner_check as owner_check
    owner_check.get_server_assets = _fake_get_server_assets
