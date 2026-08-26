# app/web/routes/capacity.py
"""용량관리(ASM/파일시스템 증설 - [예방4]) 관련 라우트"""
import logging
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.core.capacity_loader import load_capacity_items_merged
from app.core.jira_client import jira
from app.core.teams_client import send_teams_dm
from app.models.db import (
    get_capacity_input,
    get_capacity_remind_log_summary,
    log_capacity_remind,
    upsert_capacity_input,
)
from app.services.capacity import (
    build_capacity_ticket_summary,
    build_no_reply_details,
    calc_capacity_completion,
    filter_tickets_by_sheet,
)
from app.services import capacity_chatbot
from app.services.capacity_reminder import group_capacity_no_reply, group_capacity_unplanned
from app.services.capacity_report import send_capacity_report
from app.services.completion import group_by
from app.services.matcher import match_items_by_ip
from app.services.owner_check import collect_capacity_targets_with_tickets, find_owner_mismatches
from app.web.deps import require_updated_by, resolve_owner, templates

logger = logging.getLogger(__name__)

router = APIRouter()


def get_capacity_dashboard_data(sheet: str, as_of: date, use_jira: bool = True):
    items = load_capacity_items_merged(sheet=sheet)
    ticket_map = {}
    jira_error = None

    if use_jira:
        try:
            issues = jira.get_capacity_tickets()
            tickets = build_capacity_ticket_summary(issues, settings.planned_end_date_field)
            targets = [i for i in items if i["is_target"]]
            match_result = match_items_by_ip(targets, tickets)
            # 같은 서버가 DATA/ARCH 양쪽에 다 있을 수 있어, 변경작업내용으로 이 시트 소속만 남김
            ticket_map = filter_tickets_by_sheet(match_result["matched"], sheet)
        except Exception as e:
            jira_error = str(e)
            logger.warning(f"용량관리 JIRA 조회 실패: {e}")

    result = calc_capacity_completion(items, ticket_map, as_of)
    return result, jira_error


@router.get("/capacity", response_class=HTMLResponse)
def capacity_dashboard(
    request: Request,
    sheet: str = "DATA",
    team: str | None = None,
    status: str | None = None,
    q: str | None = None,
):
    if sheet not in ("DATA", "ARCH"):
        sheet = "DATA"
    today = date.today()

    result, jira_error = get_capacity_dashboard_data(sheet, today)
    by_team = group_by(result, "ops_team")

    # 증설 여부(O,X)가 공란이면서 아직 일정도 없는 '진짜 미회신' 대상만 미응답으로 (완료율
    # 분모엔 안 들어가지만 상세 목록엔 같이 보여줌). 일정이 들어온 순간부터는 status_kind가
    # "target"으로 바뀌어 result["details"]에 정상적으로 이미 포함돼 있다.
    all_items = load_capacity_items_merged(sheet=sheet)
    excluded_items = [i for i in all_items if i["status_kind"] == "excluded"]
    excluded_cnt = len(excluded_items)
    no_reply_raw = [i for i in all_items if i["status_kind"] == "no_reply"]
    no_reply_details = build_no_reply_details(no_reply_raw, today.year)

    details = result["details"] + no_reply_details
    if team:
        details = [d for d in details if d["ops_team"] == team]
        excluded_items = [d for d in excluded_items if d["ops_team"] == team]
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
                or kw in (d["ci_name"] or "").lower()
                or kw in (d["hostname"] or "").lower()
                or kw in (d["ip"] or "").lower()
            )

        details = [d for d in details if _match(d)]
        excluded_items = [d for d in excluded_items if _match(d)]

    details = sorted(details, key=lambda d: (d["schedule"] is None, d["schedule"]))

    return templates.TemplateResponse("capacity.html", {
        "request": request,
        "result": result,
        "details": details,
        "excluded_items": excluded_items,
        "excluded_cnt": excluded_cnt,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "sheet": sheet,
        "sheet_label": "DATA (ASM/파일시스템)" if sheet == "DATA" else "ARCH (아카이브)",
        "as_of": today,
        "filter_team": team or "",
        "filter_status": status or "",
        "q": q or "",
        "teams": sorted(by_team.keys()),
        "admins": sorted(settings.capacity_admin_set),
        "jira_error": jira_error,
        "jira_base": settings.jira_url.rstrip("/"),
    })


def _resolve_capacity_is_done(item_no: str, sheet: str, requested: bool, updated_by: str) -> bool:
    """완료(체크) 처리는 관리자만 변경 가능. 비관리자가 보낸 값은 무시하고 기존값 유지."""
    if updated_by in settings.capacity_admin_set:
        return requested
    existing = get_capacity_input(item_no, sheet)
    return bool(existing["is_done"]) if existing else False


@router.post("/api/capacity/save")
async def api_capacity_save(request: Request):
    """용량관리 AJAX 인라인 저장"""
    data = await request.json()
    item_no = data["item_no"]
    sheet = data["sheet"]
    updated_by = require_updated_by(data.get("updated_by", ""))
    is_done = _resolve_capacity_is_done(item_no, sheet, bool(data.get("is_done")), updated_by)
    owner = resolve_owner(data.get("owner", ""), updated_by, settings.capacity_admin_set)
    upsert_capacity_input(
        item_no=item_no,
        sheet=sheet,
        schedule=data.get("schedule", "").strip(),
        is_done=is_done,
        evidence=data.get("evidence", "").strip(),
        note=data.get("note", "").strip(),
        updated_by=updated_by,
        owner=owner,
    )
    return JSONResponse({"ok": True})


@router.post("/api/capacity/bulk-save")
async def api_capacity_bulk_save(request: Request):
    """용량관리 변경된 행만 일괄 저장. 완료값은 관리자만 반영."""
    data = await request.json()
    sheet = data["sheet"]
    updated_by = require_updated_by(data.get("updated_by", ""))
    rows = data.get("rows", [])

    for r in rows:
        item_no = r["item_no"]
        is_done = _resolve_capacity_is_done(item_no, sheet, bool(r.get("is_done")), updated_by)
        upsert_capacity_input(
            item_no=item_no,
            sheet=sheet,
            schedule=(r.get("schedule") or "").strip(),
            is_done=is_done,
            evidence=(r.get("evidence") or "").strip(),
            note=(r.get("note") or "").strip(),
            updated_by=updated_by,
        )
    return JSONResponse({"ok": True, "count": len(rows)})


@router.post("/api/capacity/exclude")
async def api_capacity_exclude(request: Request):
    """
    제외 처리/해제 (관리자만). "증설 안 함"으로 확정된 대상을 대상 목록에서 빼서
    제외 대상 섹션으로 옮긴다. excluded:false로 다시 부르면 해제(복귀) 가능.
    (엑셀 자체에 "증설 여부"가 X로 적힌 행은 이 버튼으로 해제할 수 없음 - 엑셀이 원본.)
    """
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    sheet = (data.get("sheet") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()
    excluded = bool(data.get("excluded", True))

    if not item_no or not sheet:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)
    if updated_by not in settings.capacity_admin_set:
        return JSONResponse(
            {"ok": False, "error": "제외 처리는 관리자만 가능합니다."}, status_code=403
        )

    existing = get_capacity_input(item_no, sheet) or {}
    upsert_capacity_input(
        item_no=item_no,
        sheet=sheet,
        schedule=existing.get("schedule") or "",
        is_done=bool(existing.get("is_done")),
        evidence=existing.get("evidence") or "",
        note=existing.get("note") or "",
        updated_by=updated_by,
        excluded=excluded,
    )
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# 용량관리 미계획 리마인드 미리보기 (운영팀별 초안, 발송 없음)
# DATA(일반)/ARCH(아카이브) 두 시트를 합쳐서 초안을 만든다 - 같은 서버가 양쪽에 다 있으면
# 팀당 메시지가 두 번 따로 나가던 걸 한 통으로 합치기 위함 (merge_same_server).
# ─────────────────────────────────────────────
@router.get("/capacity/remind-preview", response_class=HTMLResponse)
def capacity_remind_preview(
    request: Request,
    team: str | None = None,
    kind: str = "blank",
):
    today = date.today()
    data_result, data_err = get_capacity_dashboard_data("DATA", today)
    arch_result, arch_err = get_capacity_dashboard_data("ARCH", today)
    jira_error = data_err or arch_err
    combined_details = data_result["details"] + arch_result["details"]

    # 같은 '미계획'이라도 완전 미기입 / 대략적 일정만(예: '11월 예정') 있는 경우를 분리
    blank_groups = group_capacity_unplanned(combined_details, hinted=False)
    hinted_groups = group_capacity_unplanned(combined_details, hinted=True)
    # 미회신(증설 여부 O/X 자체가 공란)은 대상(O)이 아니라 엑셀 전체 행 기준으로 판단
    all_items = load_capacity_items_merged(sheet="DATA") + load_capacity_items_merged(sheet="ARCH")
    no_reply_groups = group_capacity_no_reply(all_items)

    groups = {"hinted": hinted_groups, "no_reply": no_reply_groups}.get(kind, blank_groups)

    # 운영팀별 발송 이력(1회라도 보냈으면 표시) - DATA/ARCH 통합 이후로는 시트 구분 없이 조회
    log_summary = get_capacity_remind_log_summary()
    for g in blank_groups + hinted_groups + no_reply_groups:
        g["sent"] = log_summary.get(g["ops_team"])

    selected = None
    if team:
        selected = next((g for g in groups if g["ops_team"] == team), None)

    return templates.TemplateResponse("capacity_remind_preview.html", {
        "request": request,
        "kind": kind,
        "groups": groups,
        "selected": selected,
        "total_unplanned": sum(g["count"] for g in groups),
        "blank_total": sum(g["count"] for g in blank_groups),
        "hinted_total": sum(g["count"] for g in hinted_groups),
        "no_reply_total": sum(g["count"] for g in no_reply_groups),
        "sender_team": settings.sender_team,
        "sender_name": settings.capacity_sender_name,
        "teams_enabled": bool(settings.teams_webhook),
        "dm_enabled": bool(settings.teams_dm_trigger_webhook),
        "jira_error": jira_error,
    })


@router.post("/api/capacity/remind-dm")
async def api_capacity_remind_dm(request: Request):
    """용량관리 미계획 리마인드 초안을 담당자에게 개인 DM 발송 (Power Automate 경유)"""
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
        log_capacity_remind(ops_team, name, team, ok, err)
    return JSONResponse({"ok": ok, "error": err})


# ─────────────────────────────────────────────
# Teams 발송
# ─────────────────────────────────────────────
@router.post("/capacity/send-report")
def trigger_capacity_report(sheet: str = Form("DATA")):
    send_capacity_report()
    return RedirectResponse(url=f"/capacity?sheet={sheet}", status_code=303)


# ─────────────────────────────────────────────
# 담당자 불일치 후보 (조직변경으로 팀명 등이 바뀌었을 가능성)
# ─────────────────────────────────────────────
@router.get("/capacity/owner-check", response_class=HTMLResponse)
def capacity_owner_check(request: Request, sheet: str = "DATA"):
    if sheet not in ("DATA", "ARCH"):
        sheet = "DATA"
    targets, ticket_map, jira_error = collect_capacity_targets_with_tickets(sheet)
    candidates = find_owner_mismatches(targets, ticket_map)

    return templates.TemplateResponse("capacity_owner_check.html", {
        "request": request,
        "sheet": sheet,
        "sheet_label": "DATA (ASM/파일시스템)" if sheet == "DATA" else "ARCH (아카이브)",
        "candidates": candidates,
        "jira_error": jira_error,
        "jira_base": settings.jira_url.rstrip("/"),
        "admins": sorted(settings.capacity_admin_set),
    })


@router.post("/api/capacity/save-owner")
async def api_capacity_save_owner(request: Request):
    """용량관리 담당자 불일치 후보에서 담당자를 직접 수정 (관리자만 가능, 엑셀 원본은 그대로 두고 DB override)"""
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    sheet = (data.get("sheet") or "").strip()
    owner = (data.get("owner") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()

    if not item_no or not sheet or not owner:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)
    if updated_by not in settings.capacity_admin_set:
        return JSONResponse(
            {"ok": False, "error": "담당자 수정은 관리자만 가능합니다."}, status_code=403
        )

    existing = get_capacity_input(item_no, sheet) or {}
    upsert_capacity_input(
        item_no=item_no,
        sheet=sheet,
        schedule=existing.get("schedule") or "",
        is_done=bool(existing.get("is_done")),
        evidence=existing.get("evidence") or "",
        note=existing.get("note") or "",
        updated_by=updated_by,
        owner=owner,
    )
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# 챗봇 (사내 LLM Agent) - 산정 기준 설명 / 진척 조회 중 선택
# ─────────────────────────────────────────────
@router.post("/api/capacity/chat")
async def api_capacity_chat(request: Request):
    data = await request.json()
    agent = (data.get("agent") or "").strip()
    query = (data.get("query") or "").strip()
    name = (data.get("name") or "").strip()

    if not query:
        return JSONResponse({"ok": False, "error": "질문을 입력해주세요"}, status_code=400)
    if agent not in ("calc", "status"):
        return JSONResponse({"ok": False, "error": "agent는 calc 또는 status여야 합니다"}, status_code=400)

    try:
        if agent == "status":
            if len(name) < 2:
                return JSONResponse(
                    {"ok": False, "error": "이름(2글자 이상)이 필요합니다"}, status_code=400
                )
            reply = capacity_chatbot.answer_status(name, query)
        else:
            reply = capacity_chatbot.answer_calc(query)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "reply": reply})
