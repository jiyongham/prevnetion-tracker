"""DR 제외 확정(관리자 X) 대상은 제외 처리일부터 완료로 친다."""
from datetime import date

import pytest

from app.config import settings
from app.services.completion import excluded_completion_date, judge


@pytest.fixture(autouse=True)
def _admins(monkeypatch):
    monkeypatch.setattr(settings, "admin_users", "관리자A")


def _item(schedule="X", by="관리자A", at="2026-10-15 09:00:00"):
    return {"no": "1", "half": "H2", "excel_done": "", "schedule_raw": schedule,
            "updated_by": by, "updated_at": at}


def test_반기_도중이라도_제외한_날부터_완료():
    completed, reason, _ = judge(_item(), [], date(2026, 10, 15), 2026)
    assert completed is True
    assert reason == "제외 확정 (2026-10-15)"


def test_제외_처리일_이전_기준으로는_소급하지_않는다():
    completed, _, _ = judge(_item(), [], date(2026, 10, 14), 2026)
    assert completed is False


def test_비관리자가_넣은_X는_제외로_보지_않는다():
    assert excluded_completion_date(_item(by="담당자")) is None
    completed, _, _ = judge(_item(by="담당자"), [], date(2026, 12, 31), 2026)
    assert completed is False


def test_X가_아니면_제외가_아니다():
    assert excluded_completion_date(_item(schedule="11/20")) is None


def test_처리일을_못_읽으면_언제_기준으로든_완료():
    completed, reason, _ = judge(_item(at=""), [], date(2026, 7, 1), 2026)
    assert completed is True
    assert reason == "제외 확정"
