# tests/test_capacity_ticket_rules.py
"""
용량관리 티켓 판별 규칙 회귀 테스트.

이 규칙들이 조용히 어긋나면 증상이 "증설을 분명히 했는데 포털에서는 미완료"로만
보이고, 원인은 코드를 읽어야만 알 수 있다. 실제로 겪은 두 가지를 못으로 박아둔다.
"""
from datetime import date

from app.services.capacity import (
    capacity_ticket_done_date,
    classify_capacity_sheet,
    judge_capacity,
    linked_issue_keys,
)


class TestClassifyCapacitySheet:
    """DATA/ARCH 영역 판별 - 한 티켓으로 두 영역을 같이 증설하는 경우가 흔하다."""

    def test_슬래시_없는_arch_표기도_arch로_본다(self):
        # DATA는 맨 단어 "DATA"도 잡는데 ARCH는 "/arch"와 "RECO"만 잡던 비대칭 때문에,
        # 이 표기가 DATA로만 분류돼 ARCH 시트에서는 완료로 안 잡혔다.
        assert classify_capacity_sheet("DATA 영역 2T, Arch 영역 1T 증설") == {"DATA", "ARCH"}

    def test_한글_아카이브_표기(self):
        assert classify_capacity_sheet("DATA 2T, 아카이브 영역 1T 증설") == {"DATA", "ARCH"}

    def test_asm_디스크그룹_표기(self):
        assert classify_capacity_sheet("+DATA 2T, +RECO 1T 증설") == {"DATA", "ARCH"}

    def test_디스크그룹_이름에_접미사가_붙어도_인식한다(self):
        # 실제 티켓은 "+DATAC1", "+RECOC1"처럼 뒤에 식별자가 붙어서 온다.
        # 한 티켓으로 두 영역을 같이 증설하는 경우라 양쪽 다 잡혀야 한다.
        assert classify_capacity_sheet("+DATAC1 2T, +RECOC1 500G 증설") == {"DATA", "ARCH"}

    def test_마운트경로_표기(self):
        assert classify_capacity_sheet("/oradata 2T, /arch 1T 증설") == {"DATA", "ARCH"}

    def test_data만_있으면_data만(self):
        assert classify_capacity_sheet("DATA 영역 500G 증설") == {"DATA"}

    def test_archive만_있으면_arch만(self):
        assert classify_capacity_sheet("Archive 영역만 1T 증설") == {"ARCH"}

    def test_database와_recovery는_영역이_아니다(self):
        # \bDATA/\bRECO에 뒤쪽 경계가 없어 흔한 일반 단어의 접두부에 걸리던 것을 막는 lookahead
        assert classify_capacity_sheet("DATABASE 재기동 후 RECOVERY 테스트") == set()


class TestCapacityTicketDoneDate:
    """완료로 볼 날짜 - 요청(SR) 티켓은 비어 있고 변경관리(CM) 티켓에만 들어간다."""

    def test_자기_완료일이_있으면_그걸_쓴다(self):
        t = {"kind": "예방4", "planned_end_date": date(2026, 9, 10), "linked": []}
        assert capacity_ticket_done_date(t) == date(2026, 9, 10)

    def test_완료일이_없으면_변경이관_티켓의_완료일을_쓴다(self):
        # SR 티켓(상태 '변경이관')은 날짜가 비어 있고 실제 작업은 CM 티켓에서 진행된다.
        # 이걸 안 따라가면 증설이 끝났는데도 '미계획'으로 남는다.
        t = {
            "kind": "증설",
            "planned_end_date": None,
            "linked": [{"key": "IMDC-30626", "planned_end_date": date(2026, 9, 28)}],
        }
        assert capacity_ticket_done_date(t) == date(2026, 9, 28)

    def test_연결_티켓도_완료일이_없으면_없음(self):
        t = {
            "kind": "증설",
            "planned_end_date": None,
            "linked": [{"key": "IMDC-30626", "planned_end_date": None}],
        }
        assert capacity_ticket_done_date(t) is None

    def test_증설_티켓이_아니면_날짜를_안_준다(self):
        t = {"kind": "기타", "planned_end_date": date(2026, 9, 10), "linked": []}
        assert capacity_ticket_done_date(t) is None


class TestNoReplyPromotion:
    """
    미회신 대상의 대상(분모) 승격 - 승격 기준은 완료 판정과 같은 함수여야 한다.

    예전엔 "티켓이 하나라도 걸렸다"로 승격시켜서, 완료로는 절대 안 잡히는 티켓까지
    분모를 늘렸다(증설한 적 없는 서버 6대가 들어와 17대가 23대가 됐다). 두 기준이
    다시 갈라지면 같은 일이 반복된다.
    """

    def _item(self, **kw):
        base = {"no": "1", "excel_done": "", "schedule_raw": ""}
        base.update(kw)
        return base

    def _ticket(self, **kw):
        # judge_capacity는 완료 근거 문구에 티켓 키를 넣으므로 key가 꼭 있어야 한다
        base = {
            "key": "IMDC-30625",
            "kind": "증설",
            "status": "완료",
            "created": "2026-09-01",
            "planned_end_date": None,
            "linked": [],
        }
        base.update(kw)
        return base

    def test_완료로_인정되는_티켓이_있으면_승격_조건을_만족한다(self):
        ticket = self._ticket(planned_end_date=date(2026, 9, 10))
        completed, reason, sel = judge_capacity(
            self._item(), [ticket], date(2026, 9, 29), 2026
        )
        assert completed is True
        assert sel is ticket
        # 근거 문구까지 확인한다 - 화면과 리포트에 그대로 나가는 문장이고,
        # 여기를 안 보면 문구를 만들다 난 오류를 테스트가 놓친다(실제로 놓쳤다).
        assert "IMDC-30625" in reason and "2026-09-10" in reason

    def test_완료일이_없는_티켓은_승격_조건을_만족하지_않는다(self):
        # 이름/IP만 스친 무관한 티켓이 분모를 늘리던 경로
        completed, _, _ = judge_capacity(
            self._item(), [self._ticket()], date(2026, 9, 29), 2026
        )
        assert completed is False

    def test_완료일이_반기_밖이면_승격되지_않는다(self):
        ticket = self._ticket(planned_end_date=date(2026, 5, 20), created="2026-05-01")
        completed, _, _ = judge_capacity(
            self._item(), [ticket], date(2026, 9, 29), 2026
        )
        assert completed is False

    def test_변경이관_티켓의_완료일로도_승격된다(self):
        # 요청(SR) 티켓엔 날짜가 없고 연결된 변경관리 티켓에만 있는 실제 구조
        ticket = self._ticket(
            status="변경이관",
            linked=[{"key": "IMDC-30626", "planned_end_date": date(2026, 9, 28)}],
        )
        completed, reason, _ = judge_capacity(
            self._item(), [ticket], date(2026, 9, 29), 2026
        )
        assert completed is True
        assert "IMDC-30626" in reason


class TestLinkedIssueKeys:
    """이슈 연결에서 같은 프로젝트(변경관리) 티켓만 따라간다."""

    def test_같은_프로젝트_링크만_남긴다(self):
        fields = {
            "issuelinks": [
                # JSM 요청 프로젝트 - 변경관리 티켓이 아니므로 제외
                {"inwardIssue": {"key": "DCIT5-19003"}},
                {"outwardIssue": {"key": "IMDC-30626"}},
            ]
        }
        assert linked_issue_keys(fields, "IMDC") == ["IMDC-30626"]

    def test_연결이_없으면_빈_목록(self):
        assert linked_issue_keys({}, "IMDC") == []
