# tests/test_kernel_excel_source.py
"""
커널 패치 대상 엑셀 읽기와, 0대로 뜰 때 원인을 알려주는 정보(describe_source).

대상이 0대인 원인은 대부분 둘 중 하나다 - 파일이 그 경로에 없거나, 머리글이 달라
모든 행이 건너뛰어진 것. 화면에서는 둘 다 똑같이 '0대'로만 보여서 구분이 안 됐다.
"""
import pandas as pd
import pytest

from app.config import settings
from app.core import kernel_loader

# 첨부된 차수 계획서 양식 그대로
ROW = {
    "서버명": "통합회원", "IP": "10.1.2.3", "호스트명": "scmat-mem-db1",
    "OS": "Linux_Redhat9.7", "시스템운영팀": "데이터센터팀", "담당자": "홍길동",
    "서버관리파트": "인프라", "작업일자": "10/06", "시간": "10:00",
    "완료여부": "완료", "Safer삭제/설치": "Safer 삭제 완료/설치 완료",
}


@pytest.fixture
def excel(tmp_path, monkeypatch):
    def _write(rows, name="커널 패치_1차.xlsx"):
        path = tmp_path / name
        pd.DataFrame(rows).to_excel(path, index=False)
        monkeypatch.setattr(settings, "kernel_dev_excel_path", str(path))
        kernel_loader._items_cache.clear()
        return path
    return _write


class TestLoadKernelItems:
    def test_차수_계획서_양식을_읽는다(self, excel):
        excel([ROW])
        item = kernel_loader.load_kernel_items("dev")[0]
        assert item["system_name"] == "통합회원"
        assert item["hostname"] == "scmat-mem-db1"
        assert item["os"] == "Linux_Redhat9.7"
        assert item["ops_team"] == "데이터센터팀"
        assert item["owner"] == "홍길동"

    def test_작업일자와_완료여부가_엑셀에서_온다(self, excel):
        # 예전 자산목록 양식엔 없던 컬럼이라, 안 읽히면 전부 '미계획'이 된다
        excel([ROW])
        item = kernel_loader.load_kernel_items("dev")[0]
        assert item["schedule_raw"] == "10/06"
        assert item["excel_done"] == "완료"
        assert item["work_time"] == "10:00"

    def test_호스트명을_키로_쓴다(self, excel):
        # 차수가 바뀌어도 같은 서버면 같은 키라야 웹 입력이 따라붙는다
        excel([ROW])
        item = kernel_loader.load_kernel_items("dev")[0]
        assert item["item_no"] == "scmat-mem-db1"

    def test_머리글_표기가_흔들려도_읽는다(self, excel):
        excel([{**{k: v for k, v in ROW.items() if k != "담당자"}, "시스템담당자": "홍길동"}])
        assert kernel_loader.load_kernel_items("dev")[0]["owner"] == "홍길동"

    def test_서버명_호스트명이_둘_다_없으면_건너뛴다(self, excel):
        excel([{"장비명": "x", "주소": "10.0.0.1"}])
        assert kernel_loader.load_kernel_items("dev") == []


class TestDescribeSource:
    def test_파일이_없으면_그대로_알려준다(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "kernel_dev_excel_path", str(tmp_path / "없는파일.xlsx"))
        info = kernel_loader.describe_source("dev")
        assert info["exists"] is False
        assert "없는파일.xlsx" in info["path"]

    def test_머리글이_안_맞으면_무엇을_못_찾았는지_알려준다(self, excel):
        excel([{"장비명": "x", "주소": "10.0.0.1"}])
        info = kernel_loader.describe_source("dev")
        assert info["exists"] is True
        assert info["rows"] == 1                      # 행은 있는데
        assert info["columns"]["system_name"] is None  # 서버명을 못 찾아 전부 건너뛴 것
        assert info["columns"]["hostname"] is None
        assert info["headers"] == ["장비명", "주소"]

    def test_정상_파일은_찾은_컬럼을_전부_돌려준다(self, excel):
        excel([ROW])
        cols = kernel_loader.describe_source("dev")["columns"]
        assert cols["system_name"] == "서버명"
        assert cols["schedule_raw"] == "작업일자"
        assert cols["excel_done"] == "완료여부"

    def test_경로가_비어_있으면_설정_없음으로_둔다(self, monkeypatch):
        monkeypatch.setattr(settings, "kernel_prod_excel_path", "")
        info = kernel_loader.describe_source("prod")
        assert info["exists"] is False and info["path"] == "(설정 없음)"
