# app/web/routes/eos.py
"""EoS(노후 OS/DB 전환 - [예방1]) 관련 라우트"""
import logging
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.core.eos_loader import load_eos_items_merged
from app.core.jira_client import jira
from app.core.teams_client import send_teams_dm
from app.models.db import (
    get_eos_input,
    get_eos_remind_log_summary,
    log_eos_remind,
    upsert_eos_input,
)
from app.services.completion import group_by
from app.services.eos import build_eos_ticket_summary, calc_eos_completion, filter_track
from app.services.eos_reminder import group_eos_no_reply, group_eos_unplanned
from app.services.eos_report import send_eos_report
from app.services.matcher import match_items_by_cmdb_key, match_items_by_ip, merge_ticket_maps
from app.services.owner_check import collect_eos_targets_with_tickets, find_owner_mismatches
from app.web.deps import require_updated_by, resolve_owner, templates

logger = logging.getLogger(__name__)

router = APIRouter()


def get_eos_dashboard_data(as_of: date, use_jira: bool = True, track: str = "ALL"):
    items = load_eos_items_merged()
    ticket_map = {}
    jira_error = None

    if use_jira:
        try:
            issues = jira.get_eos_tickets()
            tickets = build_eos_ticket_summary(issues, settings.planned_end_date_field)
            targets = [i for i in items if i["is_target"]]
            cmdb_map = match_items_by_cmdb_key(targets, tickets)
            ip_map = match_items_by_ip(targets, tickets)["matched"]
            ticket_map = merge_ticket_maps(cmdb_map, ip_map)
        except Exception as e:
            jira_error = str(e)
            logger.warning(f"EoS JIRA 조회 실패: {e}")

    result = calc_eos_completion(filter_track(items, track), ticket_map, as_of)
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

    details = result["details"]
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

    # 대상(target)이 아닌 나머지: 제외/미응답 별도로 보여줌
    all_items = load_eos_items_merged()
    excluded_cnt = sum(1 for i in all_items if i["status"] == "excluded")
    no_reply_items = [i for i in all_items if i["status"] == "no_reply"]
    if team:
        no_reply_items = [i for i in no_reply_items if i["ops_team"] == team]
    if q:
        kw = q.lower()
        no_reply_items = [
            i for i in no_reply_items
            if kw in (i["ops_team"] or "").lower()
            or kw in (i["system_name"] or "").lower()
            or kw in (i["hostname"] or "").lower()
            or kw in (i["ip"] or "").lower()
        ]

    return templates.TemplateResponse("eos.html", {
        "request": request,
        "result": result,
        "details": details,
        "no_reply_items": no_reply_items,
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
