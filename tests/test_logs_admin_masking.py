# tests/test_logs_admin_masking.py
"""
/logs (변경 이력 공용 화면)에서 관리자 이름이 변경자 라벨로 노출되지 않는지 확인.

감사 로그 원본(change_log.updated_by)은 DB에 그대로 남아야 하지만(추적용), 화면에
보여주는 '수정자' 칸은 관리자일 경우 실명 대신 '관리자'로 가려야 한다 - 사용자 요구사항
("관리자 수정은 변경자/리마인드 입력자로 보이면 안 된다")의 핵심.

conftest.py의 _isolated_test_database 픽스처가 임시 SQLite로 DB_PATH를 바꿔주므로
실제 운영 DB는 전혀 건드리지 않는다.
"""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.db import upsert_input


def test_logs_page_masks_admin_but_shows_regular_name(monkeypatch):
    monkeypatch.setattr(settings, "admin_users", "관리자A")

    # 관리자가 저장한 행과 일반 담당자가 저장한 행을 각각 하나씩 만든다.
    upsert_input(
        item_no="1", half="H2", schedule="12/25", mode="실전환",
        is_done=False, evidence="", note="", updated_by="관리자A",
    )
    upsert_input(
        item_no="2", half="H2", schedule="11/1", mode="무중단",
        is_done=False, evidence="", note="", updated_by="김담당",
    )

    client = TestClient(app)
    resp = client.get("/logs")

    assert resp.status_code == 200
    assert "김담당" in resp.text
    # 관리자 실명은 화면에 노출되지 않아야 한다 (감사 로그 DB 원본과는 별개).
    assert "관리자A" not in resp.text
    assert "관리자</td>" in resp.text


def test_logs_page_filtered_by_item_no_also_masks_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_users", "관리자A")
    upsert_input(
        item_no="1", half="H2", schedule="12/25", mode="실전환",
        is_done=False, evidence="", note="", updated_by="관리자A",
    )

    client = TestClient(app)
    resp = client.get("/logs", params={"item_no": "1"})

    assert resp.status_code == 200
    assert "관리자A" not in resp.text
