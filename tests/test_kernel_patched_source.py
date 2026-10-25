import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.main import app
from app.services import kernel_patched
from app.web.routes import kernel as kernel_routes


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture(autouse=True)
def _reset_kernel_patch_state(monkeypatch):
    kernel_patched.invalidate_patched_source_cache()
    monkeypatch.setattr(settings, "kernel_polestar_view", "")
    monkeypatch.setattr(settings, "kernel_patched_export_path", "")
    monkeypatch.setattr(kernel_patched.polestar, "base_url", "https://polestar.test")


def _write_excel(path, rows):
    pd.DataFrame(rows).to_excel(path, index=False)
    return str(path)


def test_kernel_polestar_view_검증과_v_prefix_제거():
    cfg = Settings(
        jira_url="https://jira.test",
        jira_pat="pat",
        jira_project="PRJ",
        sender_team="팀",
        sender_name="담당자",
        capacity_sender_name="용량담당자",
        dashboard_url="http://dashboard.test",
        admin_users="관리자",
        capacity_admin_users="관리자",
        eos_admin_users="관리자",
        kernel_polestar_view="V_KERNEL_PATCH",
    )
    assert cfg.kernel_polestar_view == "KERNEL_PATCH"

    with pytest.raises(ValidationError):
        Settings(
            jira_url="https://jira.test",
            jira_pat="pat",
            jira_project="PRJ",
            sender_team="팀",
            sender_name="담당자",
            capacity_sender_name="용량담당자",
            dashboard_url="http://dashboard.test",
            admin_users="관리자",
            capacity_admin_users="관리자",
            eos_admin_users="관리자",
            kernel_polestar_view="BAD-NAME",
        )


def test_read_patched_export_값컬럼이_있는데_null이면_건너뛴다(tmp_path):
    path = _write_excel(tmp_path / "patched.xlsx", [
        {"HOSTNAME": "web01", "VERSIOS": None},
        {"HOSTNAME": "web02", "VERSIOS": "null"},
        {"HOSTNAME": "web03", "VERSIOS": "   "},
    ])

    result = kernel_patched.read_patched_export(path)

    assert result["has_level"] is True
    assert result["hosts"] == set()
    assert result["levels"] == {}


def test_read_patched_export_레거시_무레벨컬럼은_목록기준_유지(tmp_path):
    path = _write_excel(tmp_path / "legacy.xlsx", [{"HOSTNAME": "web01", "IPADDRESS": "10.0.0.1"}])

    result = kernel_patched.read_patched_export(path)

    assert result["has_level"] is False
    assert result["hosts"] == {"web01"}
    assert result["ips"] == {"10.0.0.1"}


def test_read_patched_rest_대문자_versios와_ip를_읽는다(monkeypatch):
    monkeypatch.setattr(settings, "kernel_polestar_view", "KERNEL_PATCH")
    monkeypatch.setattr(
        kernel_patched.polestar.session,
        "get",
        lambda *args, **kwargs: _Resp({
            "data": {"measurement": [
                {"HOSTNAME": "WEB01", "IPADDRESS": "10.0.0.1 / 10.0.0.2", "VERSIOS": "5.14.0-687.49.1.el9_8.x86_64"}
            ]}
        }),
    )

    result = kernel_patched.read_patched_rest()

    assert result["hosts"] == {"web01"}
    assert result["ips"] == {"10.0.0.1", "10.0.0.2"}
    assert result["levels"]["web01"] == "5.14.0-687.49.1.el9_8.x86_64"


def test_read_patched_rest_중복충돌은_보수적으로_제외(monkeypatch):
    monkeypatch.setattr(settings, "kernel_polestar_view", "KERNEL_PATCH")
    monkeypatch.setattr(
        kernel_patched.polestar.session,
        "get",
        lambda *args, **kwargs: _Resp({
            "data": {"measurement": [
                {"HOSTNAME": "WEB01", "IPADDRESS": "10.0.0.1", "VERSIOS": "5.14.0-687.49.1.el9_8.x86_64"},
                {"HOSTNAME": "WEB01", "IPADDRESS": "10.0.0.1", "VERSIOS": "5.14.0-687.50.2.el9_8.x86_64"},
            ]}
        }),
    )

    result = kernel_patched.read_patched_rest()

    assert result["hosts"] == set()
    assert result["ips"] == set()
    assert result["levels"] == {}
    assert result["rows"] == 2


def test_get_patched_source_미설정이면_네트워크를_치지_않는다(monkeypatch):
    called = {"count": 0}

    def _forbidden(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("네트워크 호출이 발생하면 안 됩니다")

    monkeypatch.setattr(kernel_patched.polestar.session, "get", _forbidden)

    result = kernel_patched.get_patched_source(force_refresh=True)

    assert called["count"] == 0
    assert result["rows"] == 0


def test_get_patched_source_오류응답이면_엑셀로_폴백(monkeypatch, tmp_path):
    path = _write_excel(tmp_path / "patched.xlsx", [{"HOSTNAME": "web01", "PATCHLEVEL": "5.14.0-687.49.1.el9_8.x86_64"}])
    monkeypatch.setattr(settings, "kernel_polestar_view", "KERNEL_PATCH")
    monkeypatch.setattr(settings, "kernel_patched_export_path", path)
    monkeypatch.setattr(kernel_patched.polestar.session, "get", lambda *args, **kwargs: _Resp({"ErrorMessage": "boom"}))

    result = kernel_patched.get_patched_source(force_refresh=True)

    assert result["source_kind"] == "excel"
    assert result["hosts"] == {"web01"}


def test_get_patched_source_유효버전0건이면_엑셀로_폴백(monkeypatch, tmp_path):
    path = _write_excel(tmp_path / "patched.xlsx", [{"HOSTNAME": "web01", "PATCHLEVEL": "5.14.0-687.49.1.el9_8.x86_64"}])
    monkeypatch.setattr(settings, "kernel_polestar_view", "KERNEL_PATCH")
    monkeypatch.setattr(settings, "kernel_patched_export_path", path)
    monkeypatch.setattr(
        kernel_patched.polestar.session,
        "get",
        lambda *args, **kwargs: _Resp({"data": {"measurement": [{"HOSTNAME": "web99", "PATCHLEVEL": None}]}}),
    )

    result = kernel_patched.get_patched_source(force_refresh=True)

    assert result["source_kind"] == "excel"
    assert result["hosts"] == {"web01"}


def test_patch_levels_for_ip로_보강한다():
    result = kernel_patched.patch_levels_for(
        [{"hostname": "web01", "ip": "10.0.0.1, 10.0.0.2"}],
        {"hosts": set(), "ips": {"10.0.0.2"}, "levels": {"10.0.0.2": "5.14"}, "rows": 1, "source": "api", "has_level": True, "source_kind": "api"},
    )
    assert result == {"web01": "5.14"}


def test_kernel_summary_api가_rest_소스를_노출하고_완료를_계산한다(monkeypatch):
    monkeypatch.setattr(settings, "kernel_polestar_view", "KERNEL_PATCH")
    monkeypatch.setattr(settings, "kernel_patched_export_path", "")
    monkeypatch.setattr(kernel_routes, "load_kernel_items_merged", lambda scope="dev": [{
        "item_no": "web01",
        "no": "web01",
        "hostname": "web01",
        "ip": "10.0.0.1",
        "os": "Linux_Redhat9.7",
        "excel_done": "",
        "system_name": "WEB01",
        "ops_team": "A팀",
        "owner": "담당자",
        "scope": scope,
    }])
    monkeypatch.setattr(
        kernel_patched.polestar.session,
        "get",
        lambda *args, **kwargs: _Resp({
            "data": {"measurement": [
                {"HOSTNAME": "WEB01", "IPADDRESS": "10.0.0.1", "VERSIOS": "5.14.0-687.49.1.el9_8.x86_64"}
            ]}
        }),
    )

    resp = TestClient(app).get("/kernel/api/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["done"] == 1
    assert body["patched_source"] == "Polestar REST KERNEL_PATCH"


@pytest.mark.parametrize("response", [
    _Resp({}, status_code=500),
    _Resp(ValueError("invalid JSON")),
    _Resp({"data": {"measurement": [{"HOSTNAME": "web01", "VERSIOS": []}]}}),
])
def test_invalid_response_falls_back(monkeypatch, response):
    monkeypatch.setattr(settings, "kernel_polestar_view", "KERNEL_PATCH")
    monkeypatch.setattr(kernel_patched.polestar.session, "get", lambda *a, **kw: response)
    assert kernel_patched.get_patched_source(force_refresh=True)["rows"] == 0


def test_partial_response_and_cache_do_not_merge_excel(monkeypatch):
    monkeypatch.setattr(settings, "kernel_polestar_view", "KERNEL_PATCH")
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Accept"] == "application/json"
        return _Resp({"data": {"measurement": [
            {"HOSTNAME": "WEB01", "VERSION": "5.14.0-687.49.1.el9_8"},
            {"HOSTNAME": "WEB02", "VERSION": None},
        ]}})

    def no_excel(*args, **kwargs):
        raise AssertionError("Partial API success must not merge Excel")

    monkeypatch.setattr(kernel_patched.polestar.session, "get", fetch)
    monkeypatch.setattr(kernel_patched, "read_patched_export", no_excel)
    source = kernel_patched.get_patched_source()
    assert source["hosts"] == {"web01"}
    assert kernel_patched.patch_levels_for(
        [{"hostname": "web02"}], export=source,
    ) == {}
    kernel_patched.get_patched_source()
    assert calls == ["https://polestar.test/rest/measure/vt/KERNEL_PATCH/all"]
