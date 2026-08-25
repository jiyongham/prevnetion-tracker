# app/web/routes/eos.py
"""EoS(노후 OS/DB 전환 - [예방1]) 관련 라우트"""
import logging
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.core.date_utils import week_ranges
from app.core.eos_loader import load_eos_items_merged
from app.core.teams_client import send_teams_dm
from app.models.db import (
    add_eos_next_week_plan,
    get_eos_input,
    get_eos_next_week_plan,
    get_eos_remind_log_summary,
    log_eos_remind,
    remove_eos_next_week_plan,
    upsert_eos_input,
)
from app.services.completion import group_by
from app.services.eos import build_no_reply_details, calc_eos_completion, filter_track
from app.services.eos_data import get_eos_data
from app.services.eos_plan_chat import build_candidates, parse_plan_message
from app.services.eos_reminder import group_eos_no_reply, group_eos_unplanned
from app.services.eos_report import send_eos_report
from app.services.owner_check import collect_eos_targets_with_tickets, find_owner_mismatches
from app.web.deps import require_updated_by, resolve_owner, templates

logger = logging.getLogger(__name__)

router = APIRouter()


def get_eos_dashboard_data(as_of: date, use_jira: bool = True, track: str = "ALL"):
    items, ticket_map, polestar_confirmed, jira_error = get_eos_data(use_external=use_jira)
    result = calc_eos_completion(
        filter_track(items, track), ticket_map, as_of, polestar_confirmed=polestar_confirmed
    )
    return result, jira_error


@router.get("/eos", response_class=HTMLResponse)
def eos_dashboard(
    request: Request,
    track: str = "ALL",
    team: str | None = None,
    status: str | None = None,
    q: str | None = None,
):
    if track not in ("ALL", "OS", "DB"):
        track = "ALL"
    today = date.today()

    result, jira_error = get_eos_dashboard_data(today, track=track)
    by_team = group_by(result, "ops_team")

    all_items = load_eos_items_merged()
    excluded_cnt = sum(1 for i in all_items if i["status"] == "excluded")
    no_reply_raw = [i for i in all_items if i["status"] == "no_reply"]
    no_reply_details = build_no_reply_details(no_reply_raw, today.year)

    details = result["details"] + no_reply_details
    if team:
        details = [d for d in details if d["ops_team"] == team]
    if status == "done":
        details = [d for d in details if d["completed"]]
    elif status == "pending":
        details = [d for d in details if not d["completed"]]
    elif status == "unplanned":
        details = [d for d in details if not d["planned"]]
    if q:
        kw = q.lower()

        def _match(d):
            return (
                kw in (d["ops_team"] or "").lower()
                or kw in (d["system_name"] or "").lower()
                or kw in (d["hostname"] or "").lower()
                or kw in (d["ip"] or "").lower()
            )

        details = [d for d in details if _match(d)]

    details = sorted(details, key=lambda d: (d["schedule"] is None, d["schedule"]))

    return templates.TemplateResponse("eos.html", {
        "request": request,
        "result": result,
        "details": details,
        "excluded_cnt": excluded_cnt,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "as_of": today,
        "track": track,
        "filter_team": team or "",
        "filter_status": status or "",
        "q": q or "",
        "teams": sorted(by_team.keys()),
        "admins": sorted(settings.eos_admin_set),
        "jira_error": jira_error,
        "jira_base": settings.jira_url.rstrip("/"),
    })


def _resolve_eos_is_done(item_no: str, requested: bool, updated_by: str) -> bool:
    """완료(체크) 처리는 관리자만 변경 가능. 비관리자가 보낸 값은 무시하고 기존값 유지."""
    if updated_by in settings.eos_admin_set:
        return requested
    existing = get_eos_input(item_no)
    return bool(existing["is_done"]) if existing else False


@router.post("/api/eos/save")
async def api_eos_save(request: Request):
    """EoS AJAX 인라인 저장"""
    data = await request.json()
    item_no = data["item_no"]
    updated_by = require_updated_by(data.get("updated_by", ""))
    is_done = _resolve_eos_is_done(item_no, bool(data.get("is_done")), updated_by)
    owner = resolve_owner(data.get("owner", ""), updated_by, settings.eos_admin_set)
    upsert_eos_input(
        item_no=item_no,
        schedule=data.get("schedule", "").strip(),
        is_done=is_done,
        evidence=data.get("evidence", "").strip(),
        note=data.get("note", "").strip(),
        updated_by=updated_by,
        owner=owner,
    )
    return JSONResponse({"ok": True})


@router.post("/api/eos/bulk-save")
async def api_eos_bulk_save(request: Request):
    """EoS 변경된 행만 일괄 저장. 완료값은 관리자만 반영."""
    data = await request.json()
    updated_by = require_updated_by(data.get("updated_by", ""))
    rows = data.get("rows", [])

    for r in rows:
        item_no = r["item_no"]
        is_done = _resolve_eos_is_done(item_no, bool(r.get("is_done")), updated_by)
        upsert_eos_input(
            item_no=item_no,
            schedule=(r.get("schedule") or "").strip(),
            is_done=is_done,
            evidence=(r.get("evidence") or "").strip(),
            note=(r.get("note") or "").strip(),
            updated_by=updated_by,
        )
    return JSONResponse({"ok": True, "count": len(rows)})


# ─────────────────────────────────────────────
# EoS 미계획 리마인드 미리보기 (운영팀별 초안, 발송 없음)
# ─────────────────────────────────────────────
@router.get("/eos/remind-preview", response_class=HTMLResponse)
def eos_remind_preview(
    request: Request,
    team: str | None = None,
    kind: str = "blank",
):
    result, jira_error = get_eos_dashboard_data(date.today())

    blank_groups = group_eos_unplanned(result["details"], hinted=False)
    hinted_groups = group_eos_unplanned(result["details"], hinted=True)
    no_reply_groups = group_eos_no_reply(load_eos_items_merged())

    groups = {"hinted": hinted_groups, "no_reply": no_reply_groups}.get(kind, blank_groups)

    log_summary = get_eos_remind_log_summary()
    for g in blank_groups + hinted_groups + no_reply_groups:
        g["sent"] = log_summary.get(g["ops_team"])

    selected = None
    if team:
        selected = next((g for g in groups if g["ops_team"] == team), None)

    return templates.TemplateResponse("eos_remind_preview.html", {
        "request": request,
        "kind": kind,
        "groups": groups,
        "selected": selected,
        "total_unplanned": sum(g["count"] for g in groups),
        "blank_total": sum(g["count"] for g in blank_groups),
        "hinted_total": sum(g["count"] for g in hinted_groups),
        "no_reply_total": sum(g["count"] for g in no_reply_groups),
        "sender_team": settings.sender_team,
        "sender_name": settings.sender_name,
        "teams_enabled": bool(settings.teams_webhook),
        "dm_enabled": bool(settings.teams_dm_trigger_webhook),
        "jira_error": jira_error,
    })


@router.post("/api/eos/remind-dm")
async def api_eos_remind_dm(request: Request):
    """EoS 미계획 리마인드 초안을 담당자에게 개인 DM 발송 (Power Automate 경유)"""
    data = await request.json()
    name = (data.get("name") or "").strip()
    team = (data.get("team") or "").strip()
    message = (data.get("message") or "").strip()
    ops_team = (data.get("ops_team") or "").strip()
    if not name or not message:
        return JSONResponse(
            {"ok": False, "error": "이름과 메시지가 필요합니다."}, status_code=400
        )
    ok, err = send_teams_dm(name, team, message)
    if ops_team:
        log_eos_remind(ops_team, name, team, ok, err)
    return JSONResponse({"ok": ok, "error": err})


# ─────────────────────────────────────────────
# 담당자 불일치 후보 (조직변경으로 팀명 등이 바뀌었을 가능성)
# ─────────────────────────────────────────────
@router.get("/eos/owner-check", response_class=HTMLResponse)
def eos_owner_check(request: Request):
    targets, ticket_map, jira_error = collect_eos_targets_with_tickets()
    candidates = find_owner_mismatches(targets, ticket_map)

    return templates.TemplateResponse("eos_owner_check.html", {
        "request": request,
        "candidates": candidates,
        "jira_error": jira_error,
        "jira_base": settings.jira_url.rstrip("/"),
        "admins": sorted(settings.eos_admin_set),
    })


@router.post("/api/eos/save-owner")
async def api_eos_save_owner(request: Request):
    """EoS 담당자 불일치 후보에서 담당자를 직접 수정 (관리자만 가능, 엑셀 원본은 그대로 두고 DB override)"""
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    owner = (data.get("owner") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()

    if not item_no or not owner:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)
    if updated_by not in settings.eos_admin_set:
        return JSONResponse(
            {"ok": False, "error": "담당자 수정은 관리자만 가능합니다."}, status_code=403
        )

    existing = get_eos_input(item_no) or {}
    upsert_eos_input(
        item_no=item_no,
        schedule=existing.get("schedule") or "",
        is_done=bool(existing.get("is_done")),
        evidence=existing.get("evidence") or "",
        note=existing.get("note") or "",
        updated_by=updated_by,
        owner=owner,
    )
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# Teams 발송
# ─────────────────────────────────────────────
@router.post("/eos/send-report")
def trigger_eos_report():
    send_eos_report()
    return RedirectResponse(url="/eos", status_code=303)


# ─────────────────────────────────────────────
# 차주 계획 챗봇 (JIRA/Confluence로 못 찾은 주에 관리자가 아는 대로 자유 텍스트 입력)
# ─────────────────────────────────────────────
@router.get("/eos/plan-chat", response_class=HTMLResponse)
def eos_plan_chat_page(request: Request):
    today = date.today()
    perf_start, perf_end, plan_start, plan_end = week_ranges(today)

    saved = get_eos_next_week_plan(plan_start.isoformat())
    items = load_eos_items_merged()
    by_no = {i["item_no"]: i for i in items}
    saved_list = [
        {
            "item_no": item_no,
            "label": (by_no.get(item_no) or {}).get("system_name", item_no),
            "input_by": row.get("input_by", ""),
            "input_at": row.get("input_at", ""),
        }
        for item_no, row in saved.items()
    ]

    return templates.TemplateResponse("eos_plan_chat.html", {
        "request": request,
        "week_start": plan_start,
        "week_end": plan_end,
        "saved_list": saved_list,
        "admins": sorted(settings.eos_admin_set),
    })


@router.post("/api/eos/plan-chat/parse")
async def api_eos_plan_chat_parse(request: Request):
    """자유 텍스트 -> 언급된 것으로 보이는 EoS 대상 후보 목록"""
    data = await request.json()
    message = (data.get("message") or "").strip()
    if not message:
        return JSONResponse({"ok": False, "error": "메시지를 입력해주세요."}, status_code=400)

    items = load_eos_items_merged()
    candidates = build_candidates(items)
    try:
        matched = parse_plan_message(message, candidates)
    except Exception as e:
        logger.warning(f"EoS 차주 계획 챗봇 에이전트 호출 실패: {e}")
        return JSONResponse({"ok": False, "error": f"에이전트 호출 실패: {e}"}, status_code=502)

    return JSONResponse({"ok": True, "candidates": matched})


@router.post("/api/eos/plan-chat/save")
async def api_eos_plan_chat_save(request: Request):
    """확인된 후보들을 그 주 차주 계획 대상으로 저장 (관리자만)"""
    data = await request.json()
    updated_by = require_updated_by(data.get("updated_by", ""))
    if updated_by not in settings.eos_admin_set:
        return JSONResponse(
            {"ok": False, "error": "차주 계획 입력은 관리자만 가능합니다."}, status_code=403
        )

    item_nos = data.get("item_nos") or []
    week_start = (data.get("week_start") or "").strip()
    week_end = (data.get("week_end") or "").strip()
    if not item_nos or not week_start or not week_end:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)

    add_eos_next_week_plan(item_nos, week_start, week_end, updated_by)
    return JSONResponse({"ok": True, "count": len(item_nos)})


@router.post("/api/eos/plan-chat/remove")
async def api_eos_plan_chat_remove(request: Request):
    """잘못 추가된 항목 제거 (관리자만)"""
    data = await request.json()
    updated_by = require_updated_by(data.get("updated_by", ""))
    if updated_by not in settings.eos_admin_set:
        return JSONResponse(
            {"ok": False, "error": "차주 계획 수정은 관리자만 가능합니다."}, status_code=403
        )

    item_no = (data.get("item_no") or "").strip()
    week_start = (data.get("week_start") or "").strip()
    if not item_no or not week_start:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)

    remove_eos_next_week_plan(item_no, week_start)
    return JSONResponse({"ok": True})
