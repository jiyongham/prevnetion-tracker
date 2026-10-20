# tests/test_kernel_batch_merge.py
"""
전체 대상 목록 + 주별 차수 계획서 병합.

두 파일의 역할이 다르다:
- kernel_dev.xlsx       : 개발기 커널패치 전체 대상 (분모). 일정도 완료여부도 없다.
- 커널_패치_N차.xlsx     : 그 주에 할 서버만 추려 작업일자/완료여부를 적은 것.

차수 파일을 안 읽으면 전체 대상이 전부 '미계획'으로 뜬다.
"""
import os

import pandas as pd
import pytest

from app.config import settings
from app.core import kernel_loader

FULL = [
    {"Key": "ASSET-1", "서버명": "통합회원", "호스트명": "scmat-mem-db1", "IP": "10.1.2.3",
     "시스템운영팀": "데이터센터팀", "시스템담당자": "홍길동", "OS": "Linux_Redhat9.7"},
    {"Key": "ASSET-2", "서버명": "주문관리", "호스트명": "scmat-ord-db1", "IP": "10.1.2.4",
     "시스템운영팀": "커머스팀", "시스템담당자": "김담당", "OS": "Linux_Redhat8.10"},
]

def _batch(host, day, done="", time="10:00"):
    return {"서버명": "x", "IP": "", "호스트명": host, "OS": "", "시스템운영팀": "",
            "담당자": "", "서버관리파트": "", "작업일자": day, "시간": time,
            "완료여부": done, "Safer삭제/설치": ""}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """전체 대상 파일을 쓰고, 차수 파일은 테스트가 추가로 만든다."""
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    with pd.ExcelWriter(data / "kernel_dev.xlsx") as w:
        pd.DataFrame(FULL).to_excel(w, sheet_name="대상 서버(개발)", index=False)
    monkeypatch.setattr(settings, "kernel_dev_excel_path", "data/kernel_dev.xlsx")
    monkeypatch.setattr(settings, "kernel_batch_glob", "data/커널*패치*.xlsx")
    kernel_loader._items_cache.clear()

    def add_batch(name, rows):
        pd.DataFrame(rows).to_excel(data / name, index=False)
    return add_batch


class TestLoadKernelBatches:
    def test_차수_파일이_없으면_빈_결과(self, workspace):
        assert kernel_loader.load_kernel_batches() == {}

    def test_호스트명으로_일정과_완료여부를_모은다(self, workspace):
        workspace("커널_패치_1차.xlsx", [_batch("scmat-mem-db1", "10/06", "완료")])
        got = kernel_loader.load_kernel_batches()
        assert got["scmat-mem-db1"]["schedule_raw"] == "10/06"
        assert got["scmat-mem-db1"]["excel_done"] == "완료"
        assert got["scmat-mem-db1"]["batch"] == "커널_패치_1차"

    def test_여러_차수를_모두_읽는다(self, workspace):
        workspace("커널_패치_1차.xlsx", [_batch("scmat-mem-db1", "10/06", "완료")])
        workspace("커널_패치_2차.xlsx", [_batch("scmat-ord-db1", "10/13")])
        got = kernel_loader.load_kernel_batches()
        assert set(got) == {"scmat-mem-db1", "scmat-ord-db1"}

    def test_같은_서버가_여러_차수에_있으면_뒤_차수가_이긴다(self, workspace):
        # 재작업이면 나중 기록이 최신이다
        workspace("커널_패치_1차.xlsx", [_batch("scmat-mem-db1", "10/06")])
        workspace("커널_패치_2차.xlsx", [_batch("scmat-mem-db1", "10/13", "완료")])
        got = kernel_loader.load_kernel_batches()["scmat-mem-db1"]
        assert got["schedule_raw"] == "10/13" and got["batch"] == "커널_패치_2차"


class TestMerge:
    def test_분모는_전체_대상이고_일정은_차수에서_온다(self, workspace):
        workspace("커널_패치_1차.xlsx", [_batch("scmat-mem-db1", "10/06", "완료")])
        items = {i["hostname"]: i for i in kernel_loader.load_kernel_items_merged("dev")}

        assert len(items) == 2                       # 분모는 전체 대상 2대 그대로
        on = items["scmat-mem-db1"]
        assert on["schedule_raw"] == "10/06"         # 1차에 든 서버만 일정이 붙고
        assert on["excel_done"] == "완료"
        assert on["batch"] == "커널_패치_1차"
        off = items["scmat-ord-db1"]
        assert off["schedule_raw"] == "" and off["excel_done"] == ""   # 나머지는 미계획

    def test_차수_파일이_없으면_전부_미계획(self, workspace):
        items = kernel_loader.load_kernel_items_merged("dev")
        assert len(items) == 2
        assert all(not i["schedule_raw"] for i in items)


class TestDescribeBatches:
    def test_읽은_파일과_대수를_알려준다(self, workspace):
        workspace("커널_패치_1차.xlsx", [_batch("scmat-mem-db1", "10/06")])
        info = kernel_loader.describe_batches()
        assert info["files"] == ["커널_패치_1차.xlsx"] and info["hosts"] == 1

    def test_파일이_없으면_빈_목록(self, workspace):
        info = kernel_loader.describe_batches()
        assert info["files"] == [] and info["hosts"] == 0

    def test_이름에_공백이_있어도_읽는다(self, workspace):
        # kubectl cp 과정에서 '커널_패치_1차' -> '커널 패치_1차'로 들어간 적이 있다
        workspace("커널 패치_1차.xlsx", [_batch("scmat-mem-db1", "10/06")])
        assert kernel_loader.describe_batches()["hosts"] == 1

    def test_엑셀_잠금파일은_건너뛴다(self, workspace, tmp_path):
        # 파일을 열어둔 채 복사하면 '~$...'가 같이 딸려 들어온다 (읽으면 깨진다)
        workspace("커널_패치_1차.xlsx", [_batch("scmat-mem-db1", "10/06")])
        (tmp_path / "data" / "~$커널_패치_1차.xlsx").write_bytes(b"not-an-excel")
        info = kernel_loader.describe_batches()
        assert info["files"] == ["커널_패치_1차.xlsx"] and info["hosts"] == 1
