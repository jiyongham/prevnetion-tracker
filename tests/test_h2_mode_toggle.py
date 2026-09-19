# tests/test_h2_mode_toggle.py
"""
H1 -> H2 수행방식(mode) 토글 규칙 회귀 테스트.

배경: H2 대시보드는 더 이상 "상반기 무중단 173대만 통계, 실전환 102대는 참고
목록"으로 나뉘지 않는다. H2 대상은 H1 전체 대상(275대)과 동일하고, 각 항목의
mode는 H1의 mode+완료여부에 따라 아래 토글표대로 계산돼 강제(override)된다.

| H1 mode | H1 완료 | -> H2 mode |
|---|---|---|
| 무중단 | True  | 실전환 |
| 무중단 | False | 실전환 |
| 실전환 | True  | 무중단 |
| 실전환 | False | 실전환 (변화 없음) |

H1에 이력이 없는 항목(연도 중간 신규 시스템 등)은 토글을 적용할 기준이 없으므로
H2 시트 자체의 mode 값을 그대로 쓴다(fallback).

또한 H2의 mode는 DB(schedule_input) override로 바뀌지 않는다 - 계산값이 항상
이긴다. 이 테스트의 엑셀 픽스처 구성 방식은 (삭제된) tests/test_h2_extra_targets.py
에서 검증된 패턴을 그대로 재사용한다.
"""
import pandas as pd
import pytest

from app.core import excel_loader
from app.services import dr_data


def _make_dr_excel(path):
    """
    H1 대상 4건 (무중단×완료여부, 실전환×완료여부 4가지 조합) +
    H1에 이력이 없는 H2 신규 항목 1건(NO 5).

    - NO 1: H1 무중단, 상반기 완료 O  -> H2 실전환 기대
    - NO 2: H1 무중단, 상반기 완료 X  -> H2 실전환 기대
    - NO 3: H1 실전환, 상반기 완료 O  -> H2 무중단 기대
    - NO 4: H1 실전환, 상반기 완료 X  -> H2 실전환 기대 (변화 없음)
    - NO 5: H1에 없음(대상 아님, H2 시트에서만 신규 등장) -> H2 시트 자체 mode 유지
    """
    rows = [
        {
            "시스템명": "무중단완료", "NO": "1", "관계사": "관계사A", "주업무명": "업무A",
            "호스트명": "host-01", "IP": "********", "관리파트": "파트A",
            "APP운영팀": "운영팀A", "담당자": "홍길동", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-01", "실 전환 / 무중단": "무중단", "상반기 완료": "O",
            "하반기 일정": "2026-09-01", "실 전환 / 무중단.1": "무중단", "하반기 완료": "",
        },
        {
            "시스템명": "무중단미완료", "NO": "2", "관계사": "관계사A", "주업무명": "업무A",
            "호스트명": "host-02", "IP": "********", "관리파트": "파트A",
            "APP운영팀": "운영팀A", "담당자": "홍길동", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-02", "실 전환 / 무중단": "무중단", "상반기 완료": "",
            "하반기 일정": "2026-09-02", "실 전환 / 무중단.1": "무중단", "하반기 완료": "",
        },
        {
            "시스템명": "실전환완료", "NO": "3", "관계사": "관계사B", "주업무명": "업무B",
            "호스트명": "host-03", "IP": "********", "관리파트": "파트B",
            "APP운영팀": "운영팀B", "담당자": "김철수", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-03", "실 전환 / 무중단": "실전환", "상반기 완료": "O",
            "하반기 일정": "2026-09-03", "실 전환 / 무중단.1": "실전환", "하반기 완료": "",
        },
        {
            "시스템명": "실전환미완료", "NO": "4", "관계사": "관계사B", "주업무명": "업무B",
            "호스트명": "host-04", "IP": "********", "관리파트": "파트B",
            "APP운영팀": "운영팀B", "담당자": "김철수", "대상 여부(O,X)": "O",
            "기타 (제외 사유)": "", "증적": "",
            "상반기 일정": "2026-03-04", "실 전환 / 무중단": "실전환", "상반기 완료": "",
            "하반기 일정": "2026-09-04", "실 전환 / 무중단.1": "실전환", "하반기 완료": "",
        },
        {
            # H1에서는 대상이 아니므로(is_target=X) H1 완료판정 lookup에 no=5가 없다.
            "시스템명": "신규시스템", "NO": "5", "관계사": "관계사C", "주업무명": "업무C",
            "호스트명": "host-05", "IP": "********", "관리파트": "파트C",
            "APP운영팀": "운영팀C", "담당자": "박영희", "대상 여부(O,X)": "X",
            "기타 (제외 사유)": "상반기 미대상", "증적": "",
            "상반기 일정": "", "실 전환 / 무중단": "", "상반기 완료": "",
            "하반기 일정": "2026-09-05", "실 전환 / 무중단.1": "무중단", "하반기 완료": "",
        },
    ]
    df = pd.DataFrame(rows)
    df.to_excel(path, index=False)
    return path


@pytest.fixture()
def dr_excel_path(tmp_path, monkeypatch):
    """settings.excel_path를 테스트용 엑셀로 교체 (test_h2_extra_targets.py의 패턴 재사용)."""
    path = tmp_path / "targets.xlsx"
    _make_dr_excel(path)
    monkeypatch.setattr(excel_loader.settings, "excel_path", str(path))
    excel_loader._items_cache.clear()
    return path


def _no_to_mode(items):
    return {i["no"]: i["mode"] for i in items}


# ─────────────────────────────────────────────
# toggle_h1_to_h2_mode() - 순수 함수 단위 테스트
# ─────────────────────────────────────────────

def test_toggle_nonstop_completed_becomes_real():
    assert dr_data.toggle_h1_to_h2_mode("무중단", True) == "실전환"


def test_toggle_nonstop_incomplete_becomes_real():
    assert dr_data.toggle_h1_to_h2_mode("무중단", False) == "실전환"


def test_toggle_real_completed_becomes_nonstop():
    assert dr_data.toggle_h1_to_h2_mode("실전환", True) == "무중단"


def test_toggle_real_incomplete_stays_real():
    assert dr_data.toggle_h1_to_h2_mode("실전환", False) == "실전환"


# ─────────────────────────────────────────────
# compute_h2_items() / load_items("H2") - 통합 테스트
# ─────────────────────────────────────────────

def test_compute_h2_items_applies_toggle_table(dr_excel_path, monkeypatch):
    """4가지 조합 모두 토글표대로 계산된다 (use_jira=False로 엑셀 완료표기만 사용)."""
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    items = dr_data.compute_h2_items(use_jira=False)
    modes = _no_to_mode(items)

    assert modes["1"] == "실전환"  # 무중단 + 완료
    assert modes["2"] == "실전환"  # 무중단 + 미완료
    assert modes["3"] == "무중단"  # 실전환 + 완료
    assert modes["4"] == "실전환"  # 실전환 + 미완료 (변화 없음)


def test_compute_h2_items_fallback_for_h1_only_new_item(dr_excel_path, monkeypatch):
    """H1 이력이 없는 신규 항목(NO 5)은 토글 대신 H2 시트 자체의 mode를 그대로 쓴다."""
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    items = dr_data.compute_h2_items(use_jira=False)
    modes = _no_to_mode(items)

    assert modes["5"] == "무중단"  # H2 엑셀 시트에 적힌 그대로


def test_compute_h2_items_total_matches_h1_target_count(dr_excel_path, monkeypatch):
    """H2 대상(is_target) 수 == H1 is_target 전체 수 (더 이상 173/102로 나뉘지 않는다).

    compute_h2_items()는 load_dr_items_merged와 마찬가지로 대상 외 항목도 포함한
    전체 리스트를 반환하므로, get_targets()로 둘다 거러 대상수만 비교한다.
    """
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    from app.core.excel_loader import get_targets, load_dr_items_merged

    h1_targets = get_targets(load_dr_items_merged(half="H1"))
    h2_targets = get_targets(dr_data.compute_h2_items(use_jira=False))

    assert len(h2_targets) == len(h1_targets) == 4


def test_load_items_h2_uses_computed_mode(dr_excel_path, monkeypatch):
    """dr_data.load_items('H2')도 동일한 계산 경로(compute_h2_items)를 탄다."""
    monkeypatch.setattr(excel_loader, "get_inputs", lambda half: {})

    items = dr_data.load_items("H2")
    modes = _no_to_mode(items)

    assert modes["1"] == "실전환"
    assert modes["3"] == "무중단"


def test_h2_mode_not_overridable_by_db(dr_excel_path, monkeypatch):
    """H2 half row에 DB mode override가 있어도 계산된 토글 값이 항상 이긴다."""
    def fake_get_inputs(half):
        if half == "H2":
            # NO 1은 계산상 '실전환'이어야 하는데, DB에 '무중단'으로 저장되어 있다고 가정.
            return {"1": {
                "schedule": "", "mode": "무중단", "is_done": 0, "evidence": "",
                "owner": "", "exclude_reason": "", "note": "",
                "updated_by": "", "updated_at": "",
            }}
        return {}

    monkeypatch.setattr(excel_loader, "get_inputs", fake_get_inputs)

    items = dr_data.compute_h2_items(use_jira=False)
    modes = _no_to_mode(items)

    assert modes["1"] == "실전환"  # DB의 "무중단"이 아니라 계산값이 이긴다
