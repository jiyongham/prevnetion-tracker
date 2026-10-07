# tests/test_eos_confluence_rows.py
"""
Confluence 주간 작업계획에서 IP전환 작업행을 뽑아내는 규칙 (eos_confluence.extract_eos_rows).

이 추출이 좁으면 증상이 "금주 실적 대수가 적다"로만 보이고, 어느 구역이 통째로
빠졌는지는 페이지를 직접 열어 봐야만 알 수 있다. 실제로 구역마다 표의 칸 구성이
달라서 '4번째 칸이 작업 계획'이라는 가정이 깨졌다.
"""
from unittest.mock import patch

from app.services import eos_confluence


def _page(html: str):
    return {"body": {"storage": {"value": html}}}


def _extract(html: str):
    with patch.object(eos_confluence.confluence, "get_content", return_value=_page(html)):
        return eos_confluence.extract_eos_rows("page-1")


class TestExtractEosRows:
    def test_칸이_네_개인_기존_구조(self):
        html = """
        <h2>정기작업 (화)</h2>
        <table><tr>
          <td>1</td><td>인프라</td><td>홍길동</td>
          <td>[예방1][관계사] 통합회원 DB IP전환 작업 (11/12)</td>
        </tr></table>
        """
        rows = _extract(html)
        assert len(rows) == 1
        assert rows[0]["text"].startswith("[예방1]")
        assert rows[0]["worker"] == "홍길동"
        assert rows[0]["section"] == "정기작업 (화)"

    def test_칸이_세_개여도_읽는다(self):
        # 예전에는 len(cells) < 4면 건너뛰어서 이런 구역이 통째로 빠졌다
        html = """
        <h2>정기작업(수-목)</h2>
        <table><tr>
          <td>김담당</td><td>주문관리 DB IP전환 (11/13)</td><td>비고</td>
        </tr></table>
        """
        rows = _extract(html)
        assert len(rows) == 1
        assert rows[0]["section"] == "정기작업(수-목)"
        assert rows[0]["worker"] == "김담당"

    def test_작업계획_칸_위치가_달라도_읽는다(self):
        # 예전에는 cells[3]만 봐서, 작업 계획이 2번째 칸이면 못 읽었다
        html = """
        <h2>비정기 야간</h2>
        <table><tr>
          <td>3</td><td>정산배치 IP 전환 작업 (11/15 야간)</td><td>이담당</td><td>승인</td>
        </tr></table>
        """
        rows = _extract(html)
        assert len(rows) == 1
        assert "정산배치" in rows[0]["text"]
        assert rows[0]["section"] == "비정기 야간"

    def test_여러_구역을_모두_읽는다(self):
        html = """
        <h2>정기작업 (화)</h2>
        <table><tr><td>1</td><td>a</td><td>A</td><td>시스템1 IP전환 (11/12)</td></tr></table>
        <h2>정기작업(수-목)</h2>
        <table><tr><td>B</td><td>시스템2 IP전환 (11/13)</td></tr></table>
        <h2>비정기 야간</h2>
        <table><tr><td>2</td><td>시스템3 IP 전환 (11/15)</td><td>C</td></tr></table>
        """
        rows = _extract(html)
        assert [r["section"] for r in rows] == ["정기작업 (화)", "정기작업(수-목)", "비정기 야간"]

    def test_ip전환이_없는_행은_안_읽는다(self):
        html = """
        <h2>정기작업 (화)</h2>
        <table>
          <tr><td>1</td><td>a</td><td>A</td><td>통합회원 DB 패치 작업 (11/12)</td></tr>
          <tr><td>2</td><td>b</td><td>B</td><td>주문관리 IP전환 (11/13)</td></tr>
        </table>
        """
        rows = _extract(html)
        assert len(rows) == 1
        assert "주문관리" in rows[0]["text"]

    def test_띄어쓴_IP_전환도_읽는다(self):
        html = "<h2>비정기</h2><table><tr><td>A</td><td>시스템 IP 전환 작업</td></tr></table>"
        assert len(_extract("")) == 0
        assert len(_extract(html)) == 1

    def test_소제목이_없으면_빈_값(self):
        html = "<table><tr><td>A</td><td>시스템 IP전환</td></tr></table>"
        assert _extract(html)[0]["section"] == ""
