# app/main.py
import io
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.core.excel_loader import load_dr_items_merged, scope_h2_targets
from app.core.jira_client import jira
from app.core.scheduler import get_jobs_info, start_scheduler, stop_scheduler
from app.core.teams_client import send_teams_dm, send_teams_message
from app.models.db import get_input, get_logs, init_db, upsert_input
from app.services.completion import build_ticket_summary, calc_completion, group_by
from app.services.matcher import match_items_by_ip
from app.services import chatbot
from app.services.owner_check import (
    collect_targets_with_tickets,
    find_owner_mismatches,
    lookup_cmdb_assets,
)
from app.services.reminder import group_unplanned_by_owner
from app.services.report import get_current_half, send_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    init_db()
    logger.info("✅ DB 초기화 완료")
    start_scheduler()
    yield
    # shutdown
    stop_scheduler()


app = FastAPI(title="장애예방 활동 진척 관리", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "web" / "static")),
    name="static",
)


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
@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    half: str | None = None,
    as_of: str | None = None,
    team: str | None = None,
    status: str | None = None,
    q: str | None = None,
):
    half = half or get_current_half()
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()

    result, jira_error = get_dashboard_data(half, as_of_date)
    by_team = group_by(result, "ops_team")

    details = result["details"]
    if team:
        details = [d for d in details if d["ops_team"] == team]
    if status == "done":
        details = [d for d in details if d["completed"]]
    elif status == "pending":
        details = [d for d in details if not d["completed"]]
    elif status == "unplanned":
        # 일정 미등록 = 미계획
        details = [d for d in details if not d["planned"]]
    if q:
        kw = q.lower()
        details = [
            d for d in details
            if kw in (d["ops_team"] or "").lower()
            or kw in (d["system_name"] or "").lower()
            or kw in (d["hostname"] or "").lower()
            or kw in (d["ip"] or "").lower()
        ]

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "result": result,
        "details": details,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "half": half,
        "half_label": "상반기" if half == "H1" else "하반기",
        "as_of": as_of_date,
        "next_week": as_of_date + timedelta(days=7),
        "today": date.today(),
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
def _require_updated_by(updated_by: str) -> str:
    """변경 저장은 입력자명 2글자 이상 필수 (누가 바꿨는지 추적 가능하도록)"""
    name = (updated_by or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="입력자명을 2글자 이상 입력해주세요")
    return name


def _resolve_is_done(item_no: str, half: str, requested: bool, updated_by: str) -> bool:
    """
    완료(체크) 처리는 관리자만 변경 가능.
    비관리자가 보낸 완료 값은 무시하고 기존 저장값을 유지한다.
    """
    if updated_by in settings.admin_set:
        return requested
    existing = get_input(item_no, half)
    return bool(existing["is_done"]) if existing else False


@app.post("/api/save")
async def api_save(request: Request):
    """AJAX 인라인 저장"""
    data = await request.json()
    updated_by = _require_updated_by(data.get("updated_by", ""))
    is_done = _resolve_is_done(
        data["item_no"], data["half"], bool(data.get("is_done")), updated_by
    )
    upsert_input(
        item_no=data["item_no"],
        half=data["half"],
        schedule=data.get("schedule", "").strip(),
        mode=data.get("mode", "").strip(),
        is_done=is_done,
        evidence=data.get("evidence", "").strip(),
        note=data.get("note", "").strip(),
        updated_by=updated_by,
    )
    return JSONResponse({"ok": True})


@app.post("/api/bulk-save")
async def api_bulk_save(request: Request):
    """현재 목록의 여러 행을 한 번에 저장. 완료값은 관리자만 반영."""
    data = await request.json()
    half = data["half"]
    updated_by = _require_updated_by(data.get("updated_by", ""))
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


@app.post("/save")
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
    updated_by = _require_updated_by(updated_by)
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
@app.get("/logs", response_class=HTMLResponse)
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
@app.get("/remind-preview", response_class=HTMLResponse)
def remind_preview(
    request: Request,
    half: str | None = None,
    owner: str | None = None,
):
    half = half or get_current_half()
    result, jira_error = get_dashboard_data(half, date.today())
    unplanned = [d for d in result["details"] if not d["planned"]]
    cmdb_map = lookup_cmdb_assets(unplanned)
    groups = group_unplanned_by_owner(result["details"], cmdb_map)

    selected = None
    if owner:
        selected = next((g for g in groups if g["owner"] == owner), None)

    return templates.TemplateResponse("remind_preview.html", {
        "request": request,
        "half": half,
        "half_label": "상반기" if half == "H1" else "하반기",
        "groups": groups,
        "selected": selected,
        "total_unplanned": sum(g["count"] for g in groups),
        "sender_team": settings.sender_team,
        "sender_name": settings.sender_name,
        "teams_enabled": bool(settings.teams_webhook),
        "dm_enabled": bool(settings.teams_dm_trigger_webhook),
        "jira_error": jira_error,
    })


@app.post("/api/remind-test")
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


@app.post("/api/remind-dm")
async def api_remind_dm(request: Request):
    """미계획 리마인드 초안을 담당자에게 개인 DM 발송 (Power Automate 경유)"""
    data = await request.json()
    name = (data.get("name") or "").strip()
    team = (data.get("team") or "").strip()
    message = (data.get("message") or "").strip()
    if not name or not message:
        return JSONResponse(
            {"ok": False, "error": "이름과 메시지가 필요합니다."}, status_code=400
        )
    ok, err = send_teams_dm(name, team, message)
    return JSONResponse({"ok": ok, "error": err})


# ─────────────────────────────────────────────
# 담당자 불일치 후보 (조직변경으로 팀명 등이 바뀌었을 가능성)
# ─────────────────────────────────────────────
@app.get("/owner-check", response_class=HTMLResponse)
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
    })


# ─────────────────────────────────────────────
# 조회 챗봇 (사내 LLM Agent)
# ─────────────────────────────────────────────
@app.post("/api/chat")
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


# ─────────────────────────────────────────────
# Teams 발송
# ─────────────────────────────────────────────
@app.post("/send-report")
def trigger_report(half: str = Form(...)):
    send_report(half=half)
    return RedirectResponse(url=f"/?half={half}", status_code=303)


# ─────────────────────────────────────────────
# 엑셀 다운로드
# ─────────────────────────────────────────────
@app.get("/export")
def export_excel(half: str | None = None, as_of: str | None = None):
    half = half or get_current_half()
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
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
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────
@app.get("/api/summary")
def api_summary(half: str | None = None, as_of: str | None = None):
    half = half or get_current_half()
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    result, _ = get_dashboard_data(half, as_of_date)
    return {
        "half": half,
        "as_of": str(result["as_of"]),
        "total": result["total"],
        "done": result["done"],
        "rate": result["rate"],
        "by_team": group_by(result, "ops_team"),
    }


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": get_jobs_info()}


@app.get("/health")
def health():
    return {"status": "ok"}