# app/web/routes/dr.py
"""DR 모의훈련 관련 라우트 (대시보드/저장/리마인드/담당자확인/챗봇/AI진단/리포트/내보내기)"""
import io
import logging
from datetime import date, datetime, timedelta
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
from app.core.excel_loader import load_dr_items_merged
from app.core.jira_client import jira
from app.core.scheduler import get_jobs_info
from app.core.teams_client import send_teams_dm, send_teams_message
from app.models.db import get_input, get_logs, get_remind_log_summary, log_remind, upsert_input
from app.services import ai_diagnose, chatbot, dr_data, evidence_check, last_report
from app.services.completion import build_ticket_summary, calc_completion, group_by
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
# 대상 목록 표시 순서. '예정'과 '대략'은 같은 그룹으로 두고 날짜순으로 섞는다
# ('대략'은 월말로 근사한 schedule_sort를 쓴다 - completion.approx_schedule 참고).
STATUS_ORDER = {"완료": 0, "지연": 1, "예정": 2, "대략": 2, "미계획": 3}

# 수행방식 탭 (URL은 ASCII로, 엑셀 값은 한글)
MODE_TABS = {"real": "실전환", "nonstop": "무중단"}


def filter_by_mode(items: list[dict], mode: str | None) -> list[dict]:
    """수행방식(실전환/무중단)으로 대상을 좁힌다. mode가 없으면 전체."""
    label = MODE_TABS.get(mode or "")
    if not label:
        return items
    return [i for i in items if label in (i.get("mode") or "")]


def external_as_of(half: str) -> str:
    """외부 데이터(JIRA) 기준 시각 표시용. 갱신은 뒤에서 돌기 때문에 화면 숫자가
    몇 분 전 기준일 수 있어 언제 것인지 같이 보여준다."""
    at = dr_data.cached_at(half)
    return datetime.fromtimestamp(at).strftime("%m-%d %H:%M") if at else ""


def get_dashboard_data(half: str, as_of: date, use_jira: bool = True, mode: str | None = None):
    # ticket_map은 모드 필터 전 전체 대상으로 만들어 캐시한다 (dr_data 참고)
    items = dr_data.load_items(half)
    ticket_map, jira_error = dr_data.get_ticket_map(half, items, use_jira)
    result = calc_completion(filter_by_mode(items, mode), ticket_map, as_of)
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
    mode: str | None = None,
    report_warning: str | None = None,
    sent: str | None = None,
):
    half = half or get_current_half()
    today = date.today()
    if mode not in MODE_TABS:
        mode = None

    result, jira_error = get_dashboard_data(half, today, mode=mode)
    by_team = group_by(result, "ops_team")

    # 탭에 표시할 방식별 대수 (필터 적용 전 기준)
    scope_targets = get_dr_targets(dr_data.load_items(half))
    mode_counts = {
        "": len(scope_targets),
        **{k: len(filter_by_mode(scope_targets, k)) for k in MODE_TABS},
    }

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
    elif status in ("scheduled", "overdue", "approximate", "unplanned"):
        # 화면 5분류(완료/예정/지연/대략/미계획) 기준 필터
        label = {
            "scheduled": "예정", "overdue": "지연",
            "approximate": "대략", "unplanned": "미계획",
        }[status]
        details = [d for d in details if d["status_label"] == label]
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

    # 상태 그룹 순 -> 그룹 안에서 일정 오름차순.
    # 손댈 게 없는 것(완료)부터 손대야 하는 것(미계획)까지 순서대로 보이게 하되,
    # '예정'과 '대략'은 둘 다 앞으로 할 일이라 한 축에 놓고 날짜순으로 섞는다.
    details = sorted(details, key=lambda d: (
        STATUS_ORDER.get(d["status_label"], len(STATUS_ORDER)),
        d["schedule_sort"] or date.max,
        d["system_name"] or "",
    ))

    # 증적란에 적힌 JIRA 키의 실제 상태(반려/미종결)를 표시용으로 붙인다
    evidence_check.annotate(details)
    evidence_warn_cnt = sum(1 for d in details if d.get("evidence_level"))

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "result": result,
        "details": details,
        "excluded_items": excluded_items,
        "excluded_cnt": excluded_cnt,
        "evidence_warn_cnt": evidence_warn_cnt,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "report_warning": report_warning,
        # 발송 직후에만(sent=1) 방금 나간 본문을 화면에 띄운다
        "sent_report": last_report.get("dr") if sent else "",
        "half": half,
        "half_label": "상반기" if half == "H1" else "하반기",
        "mode": mode or "",
        "mode_label": MODE_TABS.get(mode or "", "전체"),
        "mode_counts": mode_counts,
        "mode_qs": f"&mode={mode}" if mode else "",
        "as_of": today,
        "data_as_of": external_as_of(half),
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


@router.post("/api/exclude")
async def api_exclude(request: Request):
    """
    제외 처리/해제 (관리자만). 일정 칸에 "X"를 넣는 기존 방식을 버튼으로 바꾼 것 -
    판정 로직(대시보드의 excluded_nos)은 그대로 두고 입력 수단만 추가한다.
    excluded:false로 부르면 일정을 비워 대상 목록으로 복귀시킨다.
    """
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    half = (data.get("half") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()
    excluded = bool(data.get("excluded", True))
    reason = (data.get("reason") or "").strip()

    if not item_no or not half:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)
    if updated_by not in settings.admin_set:
        return JSONResponse(
            {"ok": False, "error": "제외 처리는 관리자만 가능합니다."}, status_code=403
        )
    # 왜 뺐는지가 안 남으면 나중에 아무도 되짚을 수 없다 (제외는 분모를 바꾸는 처리)
    if excluded and not reason:
        return JSONResponse({"ok": False, "error": "제외 사유를 입력해주세요."}, status_code=400)

    existing = get_input(item_no, half) or {}
    upsert_input(
        item_no=item_no,
        half=half,
        schedule="X" if excluded else "",
        mode=existing.get("mode") or "",
        is_done=bool(existing.get("is_done")),
        evidence=existing.get("evidence") or "",
        note=existing.get("note") or "",
        updated_by=updated_by,
        exclude_reason=reason if excluded else "",  # 복귀 시엔 사유를 지운다
    )
    # 대상 구성이 바뀌었으므로 티켓 매칭 캐시를 버리고, 곧바로 뒤에서 다시 채운다
    # (버리기만 하면 제외 직후 화면을 여는 사람이 재조회를 다 기다리게 된다)
    dr_data.invalidate_cache(half)
    dr_data.prewarm(half)
    return JSONResponse({"ok": True})


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
# 리마인드 미리보기 (담당자별 초안, 발송 없음) - 미기입/대략적 일정/사전 안내 3종
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
    # 작업이 코앞인 대상은 '미계획'이 아니어서 위 두 목록에 안 잡힌다 (별도 모수)
    upcoming_groups = group_unplanned_by_service(result["details"], cmdb_map, kind="upcoming")
    groups = {"hinted": hinted_groups, "upcoming": upcoming_groups}.get(kind, blank_groups)

    # 서비스별 발송 이력. 종류별로 따로 조회해야 한다 - 미기입 리마인드를 보냈다고
    # 사전 안내까지 '발송함'으로 표시되면 실제로 안 보낸 건을 보냈다고 착각하게 된다.
    for groups_of_kind, kind_key in (
        (blank_groups, "blank"), (hinted_groups, "hinted"), (upcoming_groups, "upcoming")
    ):
        log_summary = get_remind_log_summary(half, kind_key)
        for g in groups_of_kind:
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
        "upcoming_total": sum(g["count"] for g in upcoming_groups),
        "pre_work_days": settings.pre_work_remind_days,
        "sender_team": settings.sender_team,
        "sender_name": settings.sender_name,
        "teams_enabled": bool(settings.teams_webhook),
        "dm_enabled": bool(settings.teams_dm_trigger_webhook),
        "jira_error": jira_error,
    })


@router.post("/api/remind-test")
async def api_remind_test(request: Request):
    """리마인드 초안을 Teams 웹훅으로 테스트 발송 (설정된 채널로 전송)"""
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
    """리마인드 초안을 담당자에게 개인 DM 발송 (Power Automate 경유). 발송 이력은 성공/실패 모두 기록."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    team = (data.get("team") or "").strip()
    message = (data.get("message") or "").strip()
    half = (data.get("half") or "").strip()
    service = (data.get("service") or "").strip()
    # 종류별로 이력을 따로 남긴다 (미기입/대략적 일정/사전 안내는 각각 별개의 발송)
    kind = (data.get("kind") or "blank").strip()
    if kind not in ("blank", "hinted", "upcoming"):
        kind = "blank"
    if not name or not message:
        return JSONResponse(
            {"ok": False, "error": "이름과 메시지가 필요합니다."}, status_code=400
        )
    ok, err = send_teams_dm(name, team, message)
    if half and service:
        log_remind(half, service, name, team, ok, err, kind=kind)
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
    # 수동 발송은 "지금 이 숫자를 보내겠다"는 행위라 캐시된 옛 티켓맵을 쓰면 안 된다
    dr_data.invalidate_cache(half)
    # 발송은 하되, 지난주 대비 이상 징후가 있으면 화면에 띄워 확인하게 한다
    warning = send_report(half=half)
    url = f"/?half={half}&sent=1"
    if warning:
        url += f"&report_warning={quote(warning)}"
    return RedirectResponse(url=url, status_code=303)


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
