# app/web/routes/teams.py
"""
팀별 대상 조회 - 팀 하나를 넣으면 DR훈련/용량관리/EoS/커널패치 대상 건수를 한 화면에서 본다.

지금까지는 "우리 팀이 뭘 얼마나 해야 하나"를 알려면 네 화면을 각각 열어 팀 필터를
걸어야 했다. 담당자가 가장 자주 하는 질문인데 가장 손이 많이 갔다.

각 영역의 완료율 계산 함수를 그대로 재사용한다 (포털 홈과 같은 방식) - 여기서 따로
집계하면 같은 팀 숫자가 화면마다 달라질 수 있다. 네 영역 모두 group_by(result,
"ops_team")로 팀별 {total, done}을 얻을 수 있어서, 팀 이름으로 찾아 쓰기만 하면 된다.

건수는 각 영역 대시보드의 팀 필터(?team=)로 바로 연결한다. 용량관리(DATA/ARCH)와
커널패치(개발기/운영기)처럼 화면이 갈리는 영역은 합계를 보여주고 그 아래에 화면별
링크를 따로 둔다 - 합계만 있으면 어디를 눌러야 할지 알 수 없고, 나눠서만 보여주면
"우리 팀 용량관리 몇 건"이라는 질문에 답이 안 된다.
"""
import logging
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.core.kernel_loader import available_scopes, load_kernel_items_merged
from app.services import dr_data, eos_data
from app.services.capacity import calc_capacity_completion
from app.services.completion import calc_completion, group_by
from app.services.eos import calc_eos_completion
from app.services.kernel import calc_kernel_completion
from app.services.report import get_current_half
from app.web.deps import templates
from app.web.routes.capacity import get_capacity_dashboard_data

logger = logging.getLogger(__name__)

router = APIRouter()

KERNEL_SCOPE_LABELS = {"dev": "개발기", "prod": "운영기"}


def _by_team(result: dict) -> dict[str, dict]:
    """완료율 결과 -> {팀: {total, done}}. 없는 팀은 조회할 때 0으로 처리한다."""
    return group_by(result, "ops_team")


def _dr_part(today: date) -> tuple[dict, list[dict]]:
    """DR훈련 팀별 집계. 반환: ({팀: {...}}, [화면 링크 조각])"""
    half = get_current_half()
    items = dr_data.load_items(half)
    ticket_map, _ = dr_data.get_ticket_map(half, items)
    result = calc_completion(items, ticket_map, today)
    return _by_team(result), [{"label": "", "href": f"/dr?half={half}&team="}]


def _capacity_part(today: date) -> tuple[dict, list[dict]]:
    """용량관리는 DATA/ARCH 두 시트를 합산하고, 링크는 시트별로 나눠 둔다."""
    merged: dict[str, dict] = {}
    parts = []
    for sheet, label in (("DATA", "DATA"), ("ARCH", "ARCH")):
        result, _, _ = get_capacity_dashboard_data(sheet, today)
        by_team = _by_team(result)
        for team, v in by_team.items():
            bucket = merged.setdefault(team, {"total": 0, "done": 0, "split": {}})
            bucket["total"] += v["total"]
            bucket["done"] += v["done"]
            bucket["split"][label] = v["total"]
        parts.append({"label": label, "href": f"/capacity?sheet={sheet}&team="})
    return merged, parts


def _eos_part(today: date) -> tuple[dict, list[dict]]:
    items, ticket_map, polestar_confirmed, _ = eos_data.get_eos_data()
    result = calc_eos_completion(items, ticket_map, today, polestar_confirmed=polestar_confirmed)
    return _by_team(result), [{"label": "", "href": "/eos?team="}]


def _kernel_part(today: date) -> tuple[dict, list[dict]]:
    """준비된 범위(개발기/운영기)를 합산. 운영기 확대 전에는 개발기 하나뿐이다."""
    merged: dict[str, dict] = {}
    parts = []
    for scope in available_scopes():
        label = KERNEL_SCOPE_LABELS.get(scope, scope)
        result = calc_kernel_completion(load_kernel_items_merged(scope=scope), today)
        for team, v in _by_team(result).items():
            bucket = merged.setdefault(team, {"total": 0, "done": 0, "split": {}})
            bucket["total"] += v["total"]
            bucket["done"] += v["done"]
            bucket["split"][label] = v["total"]
        parts.append({"label": label, "href": f"/kernel?scope={scope}&team="})
    return merged, parts


# 표의 열 순서 (포털 홈 카드 순서와 같게 둔다)
_DOMAINS = (
    {"code": "DR", "label": "DR훈련", "collect": _dr_part},
    {"code": "CAP", "label": "용량관리", "collect": _capacity_part},
    {"code": "EOS", "label": "EoS 전환", "collect": _eos_part},
    {"code": "KRN", "label": "커널패치", "collect": _kernel_part},
)


def collect_domains(today: date) -> tuple[list[dict], list[str]]:
    """
    네 영역의 팀별 집계를 모은다. 반환: (영역 목록, 등장하는 전체 팀 이름)

    한 영역이 실패해도(엑셀 없음/외부 조회 실패) 나머지는 보여줘야 한다 - 그 영역만
    0건으로 비우고 화면은 그대로 뜬다. 포털 홈과 같은 태도다.
    """
    domains = []
    teams: set[str] = set()
    for d in _DOMAINS:
        try:
            by_team, parts = d["collect"](today)
        except Exception as e:
            logger.warning(f"팀별 조회 {d['code']} 집계 실패: {e}")
            by_team, parts = {}, []
        domains.append({**d, "by_team": by_team, "parts": parts, "failed": not parts})
        teams.update(by_team.keys())
    return domains, sorted(teams)


def build_rows(domains: list[dict], teams: list[str]) -> list[dict]:
    """팀 목록 -> 화면에 그릴 행 (영역별 건수 + 링크)"""
    rows = []
    for team in teams:
        cells = []
        row_total = 0
        for d in domains:
            v = d["by_team"].get(team) or {"total": 0, "done": 0}
            row_total += v["total"]
            # 화면이 갈리는 영역은 split에 화면별 건수가 들어 있다. 없는 화면은 0이어야
            # 한다 - 합계를 기본값으로 두면 그 팀이 없는 시트에도 합계가 찍혀 링크를
            # 따라갔을 때 빈 목록이 나온다.
            split = v.get("split") or {}
            links = [
                {
                    "label": p["label"],
                    "count": split.get(p["label"], 0) if p["label"] else v["total"],
                    "href": p["href"] + quote(team),
                }
                for p in d["parts"]
            ]
            # 건수가 0인 화면 링크는 빼고 보여준다
            links = [x for x in links if x["count"]]
            cells.append({
                "code": d["code"],
                "total": v["total"],
                "done": v["done"],
                "links": links,
            })
        rows.append({"team": team, "cells": cells, "total": row_total})
    return rows


def _teams_url(teams: list[str]) -> str:
    """선택한 팀들로 이 화면 주소를 만든다 (팀이 없으면 전체 보기)"""
    qs = "&".join(f"team={quote(t)}" for t in teams)
    return f"/teams?{qs}" if qs else "/teams"


@router.get("/teams", response_class=HTMLResponse)
def team_lookup(request: Request, team: list[str] | None = Query(None)):
    today = date.today()
    domains, all_teams = collect_domains(today)

    known = set(all_teams)
    # 중복/오타는 조용히 버린다 (주소를 직접 고쳐 들어오는 경우가 있다)
    selected = list(dict.fromkeys(t for t in (team or []) if t in known))
    # 아무것도 안 고르면 전체를 보여준다 - 집계는 이미 다 끝나 있어서 비용이 같고,
    # 빈 화면보다 "우리 팀이 목록 어디쯤인지" 훑는 쪽이 쓸모 있다.
    rows = build_rows(domains, selected or all_teams)
    if not selected:
        rows = sorted(rows, key=lambda r: (-r["total"], r["team"]))

    # 칩의 'x'는 링크다 - 그 팀만 뺀 주소를 미리 만들어 두면 화면에서 조립할 게 없고,
    # 주소 그대로 공유해도 같은 화면이 열린다.
    chips = [
        {
            "name": t,
            "remove_url": _teams_url([x for x in selected if x != t]),
        }
        for t in selected
    ]

    return templates.TemplateResponse(request, "teams.html", {
        "request": request,
        "domains": domains,
        "rows": rows,
        "selected": selected,
        "chips": chips,
        "all_teams": all_teams,
        "as_of": today,
    })
