# tests/test_dr_match.py
"""DR훈련 티켓 매칭 - 작업 대상 CMDB 필드(CI명 + CMDB Key) 기준 (app/services/dr_match.py)."""
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


def test_CMDB_자산을_못찾으면_CI명이_호스트명과_같을때만_매칭(monkeypatch):
    _assets(monkeypatch, {})
    exact = _ticket("DR-1", ("EUVDB01", "ASSET-9"))
    partial = _ticket("DR-2", ("euvdb011", "ASSET-8"))
    assert dr_match.match_dr_tickets([_item("216", "euvdb01")], [exact, partial]) == {"216": [exact]}


def test_CMDB_CI명으로도_매칭하고_중복은_한번만(monkeypatch):
    _assets(monkeypatch, {"ecvanap01": {"object_key": "ASSET-1", "label": "유통VAN AP #1"}})
    t = _ticket("DR-1", ("유통VAN AP #1", "ASSET-1"))
    old_key = _ticket("DR-2", ("유통VAN AP #1", "ASSET-OLD-1"))
    assert dr_match.match_dr_tickets([_item("215", "ecvanap01")], [t, old_key]) == {"215": [t, old_key]}


def test_IP_호스트명_텍스트로는_매칭하지_않는다(monkeypatch):
    _assets(monkeypatch, {})
    t = _ticket("DR-1", text="ecvanap01 10.0.0.1")
    assert dr_match.match_dr_tickets([_item("215", "ecvanap01", "10.0.0.1")], [t]) == {}


def test_필드_미설정이면_조용히_0건이_아니라_오류(monkeypatch):
    monkeypatch.setattr(settings, "dr_cmdb_target_field", "")
    with pytest.raises(ValueError, match="DR_CMDB_TARGET_FIELD"):
        dr_match.match_dr_tickets([_item("215", "ecvanap01")], [])
