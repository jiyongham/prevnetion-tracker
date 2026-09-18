# tests/test_excel_loader_evidence.py
"""
DR(재해복구 모의훈련) H1/H2 반기 엑셀의 공유 '증적' 컬럼 누수 버그에 대한 회귀 테스트.

버그: load_dr_items()가 half와 무관하게 엑셀의 '증적' 컬럼을 그대로 읽어서,
H1에 입력된 증적(예: 이미 종결된 JIRA 티켓)이 H2(하반기, 상반기 무중단 대상 한정)에도
그대로 노출되던 문제. H2는 실제로 아무도 증적을 입력한 적이 없어도 evidence_check가
"OK"로 판정해버렸다.

수정: load_dr_items()에서 half == "H1"일 때만 엑셀 '증적' 컬럼을 읽고, H2는 무조건 빈
문자열로 둔다. 이후 load_dr_items_merged()가 DB(schedule_input, half='H2')에 실제
입력된 값이 있을 때만 evidence를 채워 넣는다.

Level 1: load_dr_items() 단독 테스트 (실제 DB 접근 없이 엑셀 파싱만 검증)
Level 2: load_dr_items_merged() 테스트 (DB 병합 로직까지 포함, get_inputs를 모킹)
"""
import pandas as pd
import pytest

from app.core import excel_loader
from app.core.excel_loader import load_dr_items, load_dr_items_merged

SHARED_EVIDENCE = "IMDC-99999"
DB_EVIDENCE = "IMDC-88888"


def _make_dr_excel(path):
    """load_dr_items()가 요구하는 모든 컬럼(H1+H2 공통)을 채운 테스트용 엑셀 생성."""
    df = pd.DataFrame([
        {
            "시스템명": "테스트시스템",
            "NO": "1",
            "관계사": "테스트관계사",
            "주업무명": "테스트업무",
            "호스트명": "test-host-01",
            "IP": "10.0.0.1",
            "관리파트": "테스트파트",
            "APP운영팀": "테스트운영팀",
            "담당자": "홍길동",
            "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "",
            "증적": SHARED_EVIDENCE,
            "상반기 일정": "2026-03-01",
            "실 전환 / 무중단": "무중단",
            "상반기 완료": "O",
            "하반기 일정": "2026-09-01",
            "실 전환 / 무중단.1": "무중단",
            "하반기 완료": "",
        },
    ])
    df.to_excel(path, index=False)
    return path


@pytest.fixture()
def dr_excel_path(tmp_path):
    path = tmp_path / "targets.xlsx"
    return _make_dr_excel(path)


# ─────────────────────────────────────────────
# Level 1: load_dr_items() 단독 (엑셀 파싱만)
# ─────────────────────────────────────────────

def test_h1_reads_shared_evidence_column(dr_excel_path):
    """H1은 기존 동작대로 엑셀 공유 '증적' 컬럼 값을 그대로 읽는다."""
    items = load_dr_items(excel_path=str(dr_excel_path), half="H1")
    assert len(items) == 1
    assert items[0]["evidence"] == SHARED_EVIDENCE


def test_h2_ignores_shared_evidence_column(dr_excel_path):
    """회귀 테스트: H2는 같은 엑셀의 공유 '증적' 컬럼 값을 절대 쓰지 않는다.

    수정 전에는 이 테스트가 SHARED_EVIDENCE를 반환해 실패했을 것이다.
    """
    items = load_dr_items(excel_path=str(dr_excel_path), half="H2")
    assert len(items) == 1
    assert items[0]["evidence"] == ""


# ─────────────────────────────────────────────
# Level 2: load_dr_items_merged() (DB 병합 포함)
# ─────────────────────────────────────────────

def test_h2_evidence_from_db_overrides_empty_excel(dr_excel_path, monkeypatch):
    """H2 DB에 실제 증적이 입력되어 있으면 그 값으로 채워진다 (엑셀 누수값도, 빈 값도 아님)."""
    def fake_get_inputs(half):
        return {
            "1": {
                "schedule": "",
                "mode": "",
                "is_done": 0,
                "evidence": DB_EVIDENCE,
                "owner": "",
                "exclude_reason": "",
                "note": "",
                "updated_by": "",
                "updated_at": "",
            }
        }

    monkeypatch.setattr(excel_loader, "get_inputs", fake_get_inputs)

    items = load_dr_items_merged(half="H2", excel_path=str(dr_excel_path))
    assert len(items) == 1
    assert items[0]["evidence"] == DB_EVIDENCE


def test_h2_evidence_stays_empty_without_db_input(dr_excel_path, monkeypatch):
    """핵심 회귀 테스트: DB에 H2 증적 입력이 아직 없으면(빈 dict),
    엑셀의 공유 '증적' 값이 누수되지 않고 빈 문자열로 유지되어야 한다.

    수정 전에는 여기서 SHARED_EVIDENCE가 반환되어 실패했을 것이다.
    """
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    items = load_dr_items_merged(half="H2", excel_path=str(dr_excel_path))
    assert len(items) == 1
    assert items[0]["evidence"] == ""


def test_h1_evidence_stays_from_excel_without_db_input(dr_excel_path, monkeypatch):
    """대조군: H1은 DB 입력이 없어도(빈 dict) 엑셀 공유 '증적' 값을 그대로 유지한다
    (이 수정으로 영향받지 않는 기존 동작).
    """
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    items = load_dr_items_merged(half="H1", excel_path=str(dr_excel_path))
    assert len(items) == 1
    assert items[0]["evidence"] == SHARED_EVIDENCE
