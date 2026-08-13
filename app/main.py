# app/main.py
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.core.jira_client import jira
from app.core.excel_loader import load_dr_items_merged
from app.models.db import init_db, upsert_input, get_logs
from app.services.completion import build_ticket_summary, calc_completion, group_by
from app.services.matcher import match_items_by_ip
from app.services.report import get_current_half, send_report

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="장애예방 활동 진척 관리")
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.on_event("startup")
def startup():
    init_db()
    print("✅ DB 초기화 완료")


def get_dashboard_data(half: str, as_of: date, use_jira: bool = True):
    items = load_dr_items_merged(half=half)
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

    result = calc_completion(items, ticket_map, as_of)
    return result, jira_error


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
    if q:
        kw = q.lower()
        details = [d for d in details if kw in d["system_name"].lower()
                   or kw in d["hostname"].lower() or kw in (d["ip"] or "").lower()]

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
        "jira_error": jira_error,
        "jira_base": settings.jira_url.rstrip("/"),
    })


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
    """일정 입력 저장 (폼 전송)"""
    upsert_input(
        item_no=item_no,
        half=half,
        schedule=schedule.strip(),
        mode=mode.strip(),
        is_done=(is_done == "on" or is_done == "1"),
        evidence=evidence.strip(),
        note=note.strip(),
        updated_by=updated_by.strip(),
    )
    return RedirectResponse(url=redirect_to, status_code=303)


@app.post("/api/save")
async def api_save(request: Request):
    """일정 입력 저장 (AJAX - 인라인 편집용)"""
    data = await request.json()
    upsert_input(
        item_no=data["item_no"],
        half=data["half"],
        schedule=data.get("schedule", "").strip(),
        mode=data.get("mode", "").strip(),
        is_done=bool(data.get("is_done")),
        evidence=data.get("evidence", "").strip(),
        note=data.get("note", "").strip(),
        updated_by=data.get("updated_by", "").strip(),
    )
    return JSONResponse({"ok": True})


@app.get("/logs", response_class=HTMLResponse)
def view_logs(request: Request, item_no: str | None = None):
    """변경 이력"""
    logs = get_logs(item_no=item_no, limit=200)
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": logs,
        "item_no": item_no or "",
    })


@app.post("/send-report")
def trigger_report(half: str = Form(...)):
    send_report(half=half)
    return RedirectResponse(url=f"/?half={half}", status_code=303)


@app.get("/api/summary")
def api_summary(half: str | None = None, as_of: str | None = None):
    half = half or get_current_half()
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    result, _ = get_dashboard_data(half, as_of_date)
    return {
        "half": half, "as_of": str(result["as_of"]),
        "total": result["total"], "done": result["done"], "rate": result["rate"],
        "by_team": group_by(result, "ops_team"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}
