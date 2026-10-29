# tests/test_dr_match.py
"""DR훈련 티켓 매칭 - 작업 대상 CMDB Key + IP/호스트명 텍스트의 합집합 (app/services/dr_match.py)."""
import pytest

from app.config import settings
from app.services import dr_match, owner_check

FIELD = "customfield_99999"


@pytest.fixture(autouse=True)
def _dr_cmdb_field(monkeypatch):
    monkeypatch.setattr(settings, "dr_cmdb_target_field", FIELD)
    monkeypatch.setattr(owner_check, "_cmdb_cache", {})


def _assets(monkeypatch, assets: dict):
    monkeypatch.setattr(owner_check, "get_server_assets", lambda hostnames: assets)


def _ticket(key: str, *targets: tuple[str, str], text: str = "") -> dict:
    return {
        "key": key,
        "match_text": text,
        "cmdb_targets": [{"name": name, "key": cmdb_key} for name, cmdb_key in targets],
    }


def _item(no: str, hostname: str, ip: str = "") -> dict:
    return {"no": no, "hostname": hostname, "ip": ip, "is_target": True}


def test_parse_문자열_목록과_괄호가_있는_CI명():
    assert dr_match.parse_cmdb_targets([
        "유통VAN AP #1 (Active) (ASSET-101)",
        "ecvanap02 (ASSET-102)",
    ]) == [
        {"name": "유통VAN AP #1 (Active)", "key": "ASSET-101"},
        {"name": "ecvanap02", "key": "ASSET-102"},
    ]


def test_parse_한_문자열에_여러_항목():
    assert dr_match.parse_cmdb_targets("ecvanap01 (ASSET-1), euvdb01 (ASSET-2)") == [
        {"name": "ecvanap01", "key": "ASSET-1"},
        {"name": "euvdb01", "key": "ASSET-2"},
    ]


def test_parse_Insight_객체와_Key없는_항목():
    assert dr_match.parse_cmdb_targets([
        {"label": "ecvanap01", "objectKey": "ASSET-1"},
        {"label": "euvdb01 (ASSET-2)"},
        "keyless-ci",
    ]) == [
        {"name": "ecvanap01", "key": "ASSET-1"},
        {"name": "euvdb01", "key": "ASSET-2"},
        {"name": "keyless-ci", "key": ""},
    ]


def test_build_dr_ticket_summary가_필드를_파싱한다():
    issues = [{"key": "DR-1", "fields": {
        "summary": "[예방3] 유통VAN DR훈련",
        "status": {"name": "완료"},
        FIELD: ["유통VAN AP #1 (ASSET-1)"],
    }}]
    [ticket] = dr_match.build_dr_ticket_summary(issues)
    assert ticket["cmdb_targets"] == [{"name": "유통VAN AP #1", "key": "ASSET-1"}]


def test_CMDB_Key로_매칭한다(monkeypatch):
    # 티켓에는 약칭 CI명만 있고 호스트명/IP 텍스트가 없어도 Key로 연결된다
    _assets(monkeypatch, {"ecvanap01": {"object_key": "ASSET-1", "label": "유통VAN AP #1"}})
    t = _ticket("DR-1", ("유통VAN", "ASSET-1"))
    assert dr_match.match_dr_tickets([_item("215", "ecvanap01")], [t]) == {"215": [t]}


def test_CI명으로는_매칭하지_않는다(monkeypatch):
    # 작업 대상 필드의 CI명이 호스트명과 같아도 Key가 다르면 연결하지 않는다
    _assets(monkeypatch, {"euvdb01": {"object_key": "ASSET-2", "label": "EUV DB"}})
    t = _ticket("DR-1", ("euvdb01", "ASSET-9"))
    assert dr_match.match_dr_tickets([_item("216", "euvdb01")], [t]) == {}


def test_IP_호스트명_텍스트로도_매칭한다(monkeypatch):
    _assets(monkeypatch, {})
    by_ip = _ticket("DR-1", text="변경작업 대상 10.0.0.1")
    by_host = _ticket("DR-2", text="euvdb01 전환")
    assert dr_match.match_dr_tickets([_item("216", "euvdb01", "10.0.0.1")], [by_ip, by_host]) == {
        "216": [by_ip, by_host],
    }


def test_Key와_IP_결과를_합치고_같은_티켓은_한번만(monkeypatch):
    _assets(monkeypatch, {"ecvanap01": {"object_key": "ASSET-1", "label": "유통VAN AP #1"}})
    both = _ticket("DR-1", ("유통VAN", "ASSET-1"), text="10.0.0.1")
    ip_only = _ticket("DR-2", text="10.0.0.1")
    assert dr_match.match_dr_tickets([_item("215", "ecvanap01", "10.0.0.1")], [both, ip_only]) == {
        "215": [both, ip_only],
    }


def test_필드_미설정이면_IP_호스트명으로만_매칭한다(monkeypatch):
    monkeypatch.setattr(settings, "dr_cmdb_target_field", "")

    def forbidden(hostnames):
        raise AssertionError("Key 매칭할 티켓이 없으면 CMDB를 조회하지 않는다")

    monkeypatch.setattr(owner_check, "get_server_assets", forbidden)
    t = _ticket("DR-1", text="10.0.0.1")
    assert dr_match.match_dr_tickets([_item("215", "ecvanap01", "10.0.0.1")], [t]) == {"215": [t]}
