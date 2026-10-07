# tests/test_reminder_inputter.py
"""
DR 사전 안내(작업 N일 이내) 리마인드의 수신자 판별 (app/services/reminder.py).

이 규칙이 어긋나면 "엉뚱한 사람에게 작업 안내가 갔다"로만 드러나고, 받은 쪽도
보낸 쪽도 왜 그렇게 됐는지 알기 어렵다.
"""
from app.config import settings
from app.services.reminder import inputter_of


def _item(**kw):
    base = {"input_source": "web", "updated_by": "김담당"}
    base.update(kw)
    return base


class TestInputterOf:
    def test_웹에서_일정을_넣은_사람이_입력자다(self):
        assert inputter_of(_item()) == "김담당"

    def test_엑셀_원본_일정은_입력자가_없다(self):
        # 엑셀에서 읽어온 행에도 (비고만 남긴 경우처럼) updated_by가 붙을 수 있다
        assert inputter_of(_item(input_source="excel")) == ""

    def test_관리자가_입력한_건은_입력자로_보지_않는다(self, monkeypatch):
        # 관리자는 담당자 대신 일정을 넣어주는 일이 많아 작업 당사자가 아니다.
        # 관리자에게 보내면 정작 변경 티켓을 낼 담당자가 안내를 못 받는다.
        monkeypatch.setattr(settings, "admin_users", "관리자A,관리자B")
        assert inputter_of(_item(updated_by="관리자A")) == ""
        assert inputter_of(_item(updated_by="관리자B")) == ""
        assert inputter_of(_item(updated_by="김담당")) == "김담당"

    def test_괄호_표기는_떼고_본다(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A")
        assert inputter_of(_item(updated_by="김담당(협력사)")) == "김담당"
        assert inputter_of(_item(updated_by="관리자A(인프라)")) == ""

    def test_입력자명이_비어_있으면_없음(self):
        assert inputter_of(_item(updated_by="")) == ""
