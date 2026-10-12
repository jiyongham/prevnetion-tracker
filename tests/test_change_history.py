# tests/test_change_history.py
"""
최근 변경 하이라이트 (app/services/change_history.py) 회귀 테스트.

변경자가 관리자인지 아닌지에 따라 화면에 보여줄 라벨(by_display)이 달라져야 하고,
감사 로그 원본(updated_by)은 그대로 보존되어야 한다는 게 핵심 요구사항이다.
conftest.py의 _isolated_test_database 픽스처가 각 테스트마다 임시 SQLite 파일로
DB_PATH를 바꿔주므로, 여기서 upsert_input()으로 실제로 쌓은 change_log를 그대로 쓴다
(실제 운영 DB/엑셀은 전혀 건드리지 않는다).
"""
from datetime import datetime, timedelta

from app.config import settings
from app.models.db import upsert_input
from app.services.change_history import (
    annotate_recent_changes,
    mask_changer,
)


def _detail(no="1", **kw):
    base = {"no": no}
    base.update(kw)
    return base


class TestMaskChanger:
    def test_관리자는_라벨로_가려진다(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A,관리자B")
        assert mask_changer("관리자A") == "관리자"
        assert mask_changer("관리자B") == "관리자"

    def test_비관리자는_실명_그대로(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A")
        assert mask_changer("김담당") == "김담당"

    def test_빈_값은_빈_값(self):
        assert mask_changer("") == ""
        assert mask_changer(None) == ""


class TestAnnotateRecentChanges:
    def test_최근_변경_건에_요약이_붙는다(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A")
        upsert_input(
            item_no="1", half="H2", schedule="12/25", mode="실전환",
            is_done=False, evidence="", note="", updated_by="김담당",
        )
        details = [_detail("1"), _detail("2")]
        annotate_recent_changes(details, "H2")

        changed = next(d for d in details if d["no"] == "1")
        untouched = next(d for d in details if d["no"] == "2")

        assert changed["recent_change"] is not None
        assert changed["recent_change"]["by_display"] == "김담당"
        assert "일정" in changed["recent_change"]["fields"]
        assert untouched["recent_change"] is None

    def test_관리자가_바꾼_건은_표시용_이름이_가려진다(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A")
        upsert_input(
            item_no="1", half="H2", schedule="12/25", mode="실전환",
            is_done=False, evidence="", note="", updated_by="관리자A",
        )
        details = [_detail("1")]
        annotate_recent_changes(details, "H2")

        change = details[0]["recent_change"]
        assert change["by_display"] == "관리자"
        # 감사 추적용 원본은 그대로 보존되어야 한다 (화면 표시와 별개).
        assert change["by_raw"] == "관리자A"

    def test_7일보다_오래된_변경은_표시되지_않는다(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A")
        upsert_input(
            item_no="1", half="H2", schedule="12/25", mode="실전환",
            is_done=False, evidence="", note="", updated_by="김담당",
        )
        details = [_detail("1")]
        # 변경 시점보다 10일 뒤를 '지금'으로 보고 판단하면 창(7일) 밖이라 표시되지 않아야 한다.
        future_now = datetime.now() + timedelta(days=10)
        annotate_recent_changes(details, "H2", now=future_now)

        assert details[0]["recent_change"] is None

    def test_다른_반기_변경은_섞이지_않는다(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A")
        upsert_input(
            item_no="1", half="H1", schedule="3/1", mode="무중단",
            is_done=False, evidence="", note="", updated_by="김담당",
        )
        details = [_detail("1")]
        annotate_recent_changes(details, "H2")

        assert details[0]["recent_change"] is None

    def test_여러_필드가_같이_바뀌면_한_번의_변경으로_묶인다(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_users", "관리자A")
        upsert_input(
            item_no="1", half="H2", schedule="12/25", mode="실전환",
            is_done=True, evidence="TICKET-1", note="메모", updated_by="김담당",
        )
        details = [_detail("1")]
        annotate_recent_changes(details, "H2")

        change = details[0]["recent_change"]
        # 같은 저장 1번으로 여러 필드가 한꺼번에 바뀌었으므로 묶여서 표시되어야 한다.
        assert len(change["fields"]) >= 2
        assert "일정" in change["fields"]

    def test_db_조회_실패해도_예외를_올리지_않는다(self, monkeypatch):
        """DB 조회 실패는 전체 화면을 막으면 안 되므로 조용히 생략한다."""
        from app.services import change_history as ch_module

        def boom(*args, **kwargs):
            raise RuntimeError("DB 연결 실패")

        # change_history.py가 `from app.models.db import get_recent_logs_by_half`로
        # 이름을 직접 바인딩했으므로, 패치 대상도 change_history 모듈 쪽이어야 한다.
        monkeypatch.setattr(ch_module, "get_recent_logs_by_half", boom)
        details = [_detail("1")]
        result = annotate_recent_changes(details, "H2")
        assert result is details  # 원본 그대로 반환 (recent_change 필드 없이)
