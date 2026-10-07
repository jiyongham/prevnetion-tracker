# tests/test_reminder_body.py
"""
DR 리마인드 본문의 수행방식 구분 (app/services/reminder.py).

한 담당자가 실전환과 무중단을 같이 맡는 경우가 있는데 준비할 내용이 서로 달라서,
목록이 섞여 나가면 받는 쪽이 어느 게 어느 건지 구분할 수 없다.
"""
from app.services.reminder import build_body, group_by_mode, mode_of


def _item(system, mode, **kw):
    base = {"system_name": system, "hostname": f"{system}-h", "ip": "10.0.0.1", "mode": mode}
    base.update(kw)
    return base


class TestModeOf:
    def test_표기에_꼬리가_붙어도_같은_방식으로_본다(self):
        assert mode_of({"mode": "실전환(예정)"}) == "실전환"
        assert mode_of({"mode": "무중단"}) == "무중단"

    def test_방식이_없으면_미정(self):
        assert mode_of({"mode": ""}) == "방식 미정"
        assert mode_of({}) == "방식 미정"


class TestGroupByMode:
    def test_실전환이_무중단보다_먼저_온다(self):
        items = [_item("B", "무중단"), _item("A", "실전환")]
        assert [m for m, _ in group_by_mode(items)] == ["실전환", "무중단"]

    def test_방식_미정은_맨_뒤(self):
        items = [_item("C", ""), _item("A", "실전환")]
        assert [m for m, _ in group_by_mode(items)] == ["실전환", "방식 미정"]

    def test_같은_방식은_한_묶음으로(self):
        items = [_item("A", "실전환"), _item("B", "무중단"), _item("C", "실전환")]
        groups = dict(group_by_mode(items))
        assert [d["system_name"] for d in groups["실전환"]] == ["A", "C"]
        assert [d["system_name"] for d in groups["무중단"]] == ["B"]


class TestBuildBody:
    def test_방식별로_나눠_적는다(self):
        body = build_body([_item("A", "실전환"), _item("B", "무중단")])
        assert body == (
            "[실전환]\n"
            "- A / A-h / 10.0.0.1\n"
            "\n"
            "[무중단]\n"
            "- B / B-h / 10.0.0.1"
        )

    def test_줄_앞에_공백이_없다(self):
        # 줄 앞 공백/탭이 있으면 Teams가 그 줄을 코드블록으로 잡아 본문이 갈라진다
        body = build_body([_item("A", "실전환"), _item("B", "무중단")])
        assert all(line == line.lstrip() for line in body.splitlines())

    def test_등록된_일정도_같이_보여줄_수_있다(self):
        item = _item("A", "실전환", schedule_raw="11월 예정", status_label="대략")
        assert "(현재 등록: 11월 예정)" in build_body([item], show_raw=True)

    def test_예정_대상은_정규화된_날짜로(self):
        item = _item("A", "무중단", schedule_raw="2026-11-03", status_label="예정",
                     schedule_disp="11/3")
        assert "(예정: 11/3)" in build_body([item], show_raw=True)

    def test_대상이_없으면_안내_문구(self):
        assert build_body([]) == "(대상 없음)"
