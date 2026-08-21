# app/web/routes/dr.py
"""DR 모의훈련 관련 라우트 (대시보드/저장/리마인드/담당자확인/챗봇/AI진단/리포트/내보내기)"""
import io
import logging
from datetime import date, timedelta
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from app.config import settings
from app.core.excel_loader import get_targets as get_dr_targets
from app.core.excel_loader import load_dr_items_merged, scope_h2_targets
from app.core.jira_client import jira
from app.core.scheduler import get_jobs_info
from app.core.teams_client import send_teams_dm, send_teams_message
from app.models.db import get_input, get_logs, get_remind_log_summary, log_remind, upsert_input
from app.services import ai_diagnose, chatbot
from app.services.completion import build_ticket_summary, calc_completion, group_by
from app.services.matcher import match_items_by_ip
from app.services.owner_check import (
    collect_targets_with_tickets,
    find_owner_mismatches,
    lookup_cmdb_assets,
)
from app.services.reminder import group_unplanned_by_service
from app.services.report import get_current_half, send_report
from app.web.deps import require_updated_by, resolve_owner, templates

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────────
def get_dashboard_data(half: str, as_of: date, use_jira: bool = True):
    items = load_dr_items_merged(half=half)
    # 하반기는 상반기 무중단 대상(174대)에 한해 수행 → 분모/상세목록 한정
    if half == "H2":
        items = scope_h2_targets(items)
    ticket_map = {}
    jira_error = None

    if use_jira:
        try:
            issues = jira.get_dr_tickets()
            tickets = build_ticket_summary(issues, settings.planned_end_date_field)
            targets = [i for i in items if i["is_target"]]
            match_result = match_items_by_ip(targets, tickets)
            ticket_map = match_result["matched"]
        except Exception as e:
            jira_error = str(e)
            logger.warning(f"JIRA 조회 실패: {e}")

    result = calc_completion(items, ticket_map, as_of)
    return result, jira_error


# ─────────────────────────────────────────────
# 대시보드
# ─────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    half: str | None = None,
    team: str | None = None,
    status: str | None = None,
    q: str | None = None,
):
    half = half or get_current_half()
    today = date.today()

    result, jira_error = get_dashboard_data(half, today)
    by_team = group_by(result, "ops_team")

    # 대상 여부(O,X)에서 X로 제외된 건수. Teams 리포트 "1. 전체 대상"과 같은 기준으로,
    # 상반기 무중단 174대로 좁히기 전 그 반기 엑셀 전체를 기준으로 센다 (좁힌 뒤로 세면
    # H2는 174대 전원이 이미 O라서 제외가 늘 0으로 나와 의미가 없다).
    half_items = load_dr_items_merged(half=half)
    excluded_cnt = len(half_items) - len(get_dr_targets(half_items))

    details = result["details"]
    if team:
        details = [d for d in details if d["ops_team"] == team]

    # 일정 칸에 'X'로 기입된 항목 = 제외 대상으로 별도 분류 (완료/미완료/미계획 목록에선 제외).
    # 단, 관리자가 웹에서 직접 처리(X 입력+저장)한 경우만 포함한다 — 비관리자가 실수로 입력했거나
    # 엑셀 원본에 그냥 X라고만 적혀있는 건(누가 처리했는지 확인 불가) 제외 대상으로 안 본다.
    excluded_nos = {
        d["no"] for d in details
        if (d.get("schedule_raw") or "").strip().upper() == "X"
        and d.get("updated_by") in settings.admin_set
    }
    excluded_items = [d for d in details if d["no"] in excluded_nos]
    details = [d for d in details if d["no"] not in excluded_nos]

    if status == "done":
        details = [d for d in details if d["completed"]]
    elif status == "pending":
        details = [d for d in details if not d["completed"]]
    elif status == "unplanned":
        # 일정 미등록 = 미계획
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
        excluded_items = [d for d in excluded_items if _match(d)]

    # 일정 오름차순 (일정 없는 미계획 대상은 뒤로)
    details = sorted(details, key=lambda d: (d["schedule"] is None, d["schedule"]))

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "result": result,
        "details": details,
        "excluded_items": excluded_items,
        "excluded_cnt": excluded_cnt,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "half": half,
        "half_label": "상반기" if half == "H1" else "하반기",
        "as_of": today,
        "next_week": today + timedelta(days=7),
        "today": today,
        "filter_team": team or "",
        "filter_status": status or "",
        "q": q or "",
        "teams": sorted(by_team.keys()),
        "admins": sorted(settings.admin_set),
        "jira_error": jira_error,
        "jira_base": settings.jira_url.rstrip("/"),
        "jobs": get_jobs_info(),
    })


# ─────────────────────────────────────────────
# 일정 입력 저장
# ─────────────────────────────────────────────
def _resolve_is_done(item_no: str, half: str, requested: bool, updated_by: str) -> bool:
    """
    완료(체크) 처리는 관리자만 변경 가능.
    비관리자가 보낸 완료 값은 무시하고 기존 저장값을 유지한다.
    """
    if updated_by in settings.admin_set:
        return requested
    existing = get_input(item_no, half)
    return bool(existing["is_done"]) if existing else False


@router.post("/api/save")
async def api_save(request: Request):
    """AJAX 인라인 저장"""
    data = await request.json()
    updated_by = require_updated_by(data.get("updated_by", ""))
    is_done = _resolve_is_done(
        data["item_no"], data["half"], bool(data.get("is_done")), updated_by
    )
    owner = resolve_owner(data.get("owner", ""), updated_by, settings.admin_set)
    upsert_input(
        item_no=data["item_no"],
        half=data["half"],
        schedule=data.get("schedule", "").strip(),
        mode=data.get("mode", "").strip(),
        is_done=is_done,
        evidence=data.get("evidence", "").strip(),
        note=data.get("note", "").strip(),
        updated_by=updated_by,
        owner=owner,
    )
    return JSONResponse({"ok": True})


@router.post("/api/bulk-save")
async def api_bulk_save(request: Request):
    """현재 목록의 여러 행을 한 번에 저장. 완료값은 관리자만 반영."""
    data = await request.json()
    half = data["half"]
    updated_by = require_updated_by(data.get("updated_by", ""))
    rows = data.get("rows", [])

    for r in rows:
        item_no = r["item_no"]
        is_done = _resolve_is_done(
            item_no, half, bool(r.get("is_done")), updated_by
        )
        upsert_input(
            item_no=item_no,
            half=half,
            schedule=(r.get("schedule") or "").strip(),
            mode=(r.get("mode") or "").strip(),
            is_done=is_done,
            evidence=(r.get("evidence") or "").strip(),
            note=(r.get("note") or "").strip(),
            updated_by=updated_by,
        )
    return JSONResponse({"ok": True, "count": len(rows)})


@router.post("/save")
def save_schedule(
    item_no: str = Form(...),
    half: str = Form(...),
    schedule: str = Form(""),
    mode: str = Form(""),
    is_done: str = Form(""),
    evidence: str = Form(""),
    note: str = Form(""),
    updated_by: str = Form(""),
    redirect_to: str = Form("/"),
):
    """폼 전송 저장"""
    updated_by = require_updated_by(updated_by)
    done_requested = is_done in ("on", "1", "true")
    upsert_input(
        item_no=item_no,
        half=half,
        schedule=schedule.strip(),
        mode=mode.strip(),
        is_done=_resolve_is_done(item_no, half, done_requested, updated_by),
        evidence=evidence.strip(),
        note=note.strip(),
        updated_by=updated_by,
    )
    return RedirectResponse(url=redirect_to, status_code=303)


# ─────────────────────────────────────────────
# 변경 이력
# ─────────────────────────────────────────────
@router.get("/logs", response_class=HTMLResponse)
def view_logs(request: Request, item_no: str | None = None):
    logs = get_logs(item_no=item_no, limit=200)
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": logs,
        "item_no": item_no or "",
    })


# ─────────────────────────────────────────────
# 미계획 리마인드 미리보기 (담당자별 초안, 발송 없음)
# ─────────────────────────────────────────────
@router.get("/remind-preview", response_class=HTMLResponse)
def remind_preview(
    request: Request,
    half: str | None = None,
    service: str | None = None,
    kind: str = "blank",
):
    half = half or get_current_half()
    result, jira_error = get_dashboard_data(half, date.today())
    unplanned = [d for d in result["details"] if not d["planned"]]
    cmdb_map = lookup_cmdb_assets(unplanned)

    # 같은 '미계획'이라도 완전 미기입 / 대략적 일정만(예: '11월 예정') 있는 경우를 분리
    blank_groups = group_unplanned_by_service(result["details"], cmdb_map, hinted=False)
    hinted_groups = group_unplanned_by_service(result["details"], cmdb_map, hinted=True)
    groups = hinted_groups if kind == "hinted" else blank_groups

    # 서비스별 발송 이력(1회라도 보냈으면 표시)
    log_summary = get_remind_log_summary(half)
    for g in blank_groups + hinted_groups:
        g["sent"] = log_summary.get(g["service"])

    selected = None
    if service:
        selected = next((g for g in groups if g["service"] == service), None)

    return templates.TemplateResponse("remind_preview.html", {
        "request": request,
        "half": half,
        "half_label": "상반기" if half == "H1" else "하반기",
        "kind": kind,
        "groups": groups,
        "selected": selected,
        "total_unplanned": sum(g["count"] for g in groups),
        "blank_total": sum(g["count"] for g in blank_groups),
        "hinted_total": sum(g["count"] for g in hinted_groups),
        "sender_team": settings.sender_team,
        "sender_name": settings.sender_name,
        "teams_enabled": bool(settings.teams_webhook),
        "dm_enabled": bool(settings.teams_dm_trigger_webhook),
        "jira_error": jira_error,
    })


@router.post("/api/remind-test")
async def api_remind_test(request: Request):
    """미계획 리마인드 초안을 Teams 웹훅으로 테스트 발송 (설정된 채널로 전송)"""
    data = await request.json()
    message = (data.get("message") or "").strip()
    if not message:
        return JSONResponse({"ok": False, "error": "메시지가 비어 있습니다."}, status_code=400)
    if not settings.teams_webhook:
        return JSONResponse(
            {"ok": False, "error": "TEAMS_WEBHOOK 미설정 (.env 확인)"}, status_code=400
        )
    ok = send_teams_message(message)
    return JSONResponse({"ok": ok})


@router.post("/api/remind-dm")
async def api_remind_dm(request: Request):
    """미계획 리마인드 초안을 담당자에게 개인 DM 발송 (Power Automate 경유). 발송 이력은 성공/실패 모두 기록."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    team = (data.get("team") or "").strip()
    message = (data.get("message") or "").strip()
    half = (data.get("half") or "").strip()
    service = (data.get("service") or "").strip()
    if not name or not message:
        return JSONResponse(
            {"ok": False, "error": "이름과 메시지가 필요합니다."}, status_code=400
        )
    ok, err = send_teams_dm(name, team, message)
    if half and service:
        log_remind(half, service, name, team, ok, err)
    return JSONResponse({"ok": ok, "error": err})


# ─────────────────────────────────────────────
# 담당자 불일치 후보 (조직변경으로 팀명 등이 바뀌었을 가능성)
# ─────────────────────────────────────────────
@router.get("/owner-check", response_class=HTMLResponse)
def owner_check(request: Request, half: str | None = None):
    half = half or get_current_half()
    targets, ticket_map, jira_error = collect_targets_with_tickets(half)
    candidates = find_owner_mismatches(targets, ticket_map)

    return templates.TemplateResponse("owner_check.html", {
        "request": request,
        "half": half,
        "half_label": "상반기" if half == "H1" else "하반기",
        "candidates": candidates,
        "jira_error": jira_error,
        "jira_base": settings.jira_url.rstrip("/"),
        "admins": sorted(settings.admin_set),
    })


@router.post("/api/save-owner")
async def api_save_owner(request: Request):
    """담당자 불일치 후보에서 담당자를 직접 수정 (관리자만 가능, 엑셀 원본은 그대로 두고 DB override)"""
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    half = (data.get("half") or "").strip()
    owner = (data.get("owner") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()

    if not item_no or not half or not owner:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)
    if updated_by not in settings.admin_set:
        return JSONResponse(
            {"ok": False, "error": "담당자 수정은 관리자만 가능합니다."}, status_code=403
        )

    # owner 외 필드는 기존 값을 그대로 유지 (안 넘기면 upsert_input이 빈 값으로 덮어씀)
    existing = get_input(item_no, half) or {}
    upsert_input(
        item_no=item_no,
        half=half,
        schedule=existing.get("schedule") or "",
        mode=existing.get("mode") or "",
        is_done=bool(existing.get("is_done")),
        evidence=existing.get("evidence") or "",
        note=existing.get("note") or "",
        updated_by=updated_by,
        owner=owner,
    )
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# 조회 챗봇 (사내 LLM Agent)
# ─────────────────────────────────────────────
@router.post("/api/chat")
async def api_chat(request: Request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    query = (data.get("query") or "").strip()
    half = data.get("half") or get_current_half()

    if len(name) < 2 or not query:
        return JSONResponse(
            {"ok": False, "error": "이름(2글자 이상)과 질문이 필요합니다"}, status_code=400
        )

    result, _ = get_dashboard_data(half, date.today())
    try:
        reply = chatbot.answer(name, query, result["details"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "reply": reply})


@router.post("/api/diagnose-unmatched")
async def api_diagnose_unmatched(request: Request):
    """JIRA 매칭이 안 된 대상 하나에 대해, 버튼 클릭 시에만 AI 진단 (자동/일괄 실행 없음)"""
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    half = data.get("half") or get_current_half()

    result, _ = get_dashboard_data(half, date.today())
    item = next((d for d in result["details"] if d["no"] == item_no), None)
    if not item:
        return JSONResponse({"ok": False, "error": "대상을 찾을 수 없습니다"}, status_code=404)

    try:
        issues = jira.get_dr_tickets()
        tickets = build_ticket_summary(issues, settings.planned_end_date_field)
        candidates = ai_diagnose.find_candidate_tickets(item, tickets)
        reply = ai_diagnose.diagnose_unmatched(item, candidates)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "reply": reply})


@router.post("/api/diagnose-mismatch")
async def api_diagnose_mismatch(request: Request):
    """담당자 불일치 후보 한 건에 대해, 버튼 클릭 시에만 AI 판단 (자동/일괄 실행 없음)"""
    data = await request.json()
    try:
        reply = ai_diagnose.diagnose_mismatch(data)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "reply": reply})


# ─────────────────────────────────────────────
# Teams 발송
# ─────────────────────────────────────────────
@router.post("/send-report")
def trigger_report(half: str = Form(...)):
    send_report(half=half)
    return RedirectResponse(url=f"/?half={half}", status_code=303)


# ─────────────────────────────────────────────
# 엑셀 다운로드
# ─────────────────────────────────────────────
@router.get("/export")
def export_excel(half: str | None = None):
    half = half or get_current_half()
    as_of_date = date.today()
    result, _ = get_dashboard_data(half, as_of_date)

    rows = []
    for d in result["details"]:
        rows.append({
            "NO": d["no"],
            "관계사": d.get("company", ""),
            "시스템명": d["system_name"],
            "호스트명": d["hostname"],
            "IP": d["ip"],
            "APP운영팀": d["ops_team"],
            "담당자": d["owner"],
            "일정": d["schedule_disp"],
            "실전환/무중단": d["mode"],
            "완료": "O" if d["completed"] else "",
            "JIRA": d.get("jira_key", ""),
            "판정근거": d.get("reason", ""),
        })

    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=f"DR_{half}")
    buf.seek(0)

    fname = f"DR_진척_{half}_{as_of_date}.xlsx"
    fname_encoded = quote(fname)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=\"DR_export_{half}_{as_of_date}.xlsx\"; "
            f"filename*=UTF-8''{fname_encoded}"
        },
    )


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────
@router.get("/api/summary")
def api_summary(half: str | None = None):
    half = half or get_current_half()
    result, _ = get_dashboard_data(half, date.today())
    return {
        "half": half,
        "as_of": str(result["as_of"]),
        "total": result["total"],
        "done": result["done"],
        "rate": result["rate"],
        "by_team": group_by(result, "ops_team"),
    }
