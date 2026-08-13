# app/main.py
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.core.jira_client import jira
from app.core.excel_loader import load_dr_items, HALF_COLS
from app.services.completion import build_ticket_summary, calc_completion, group_by
from app.services.matcher import match_items_by_ip
from app.services.report import get_current_half, send_report

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="장애예방 활동 진척 관리")
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


def get_dashboard_data(half: str, as_of: date, use_jira: bool = True):
    """대시보드용 데이터 수집"""
    items = load_dr_items(half=half)
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
):
    half = half or get_current_half()
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()

    result, jira_error = get_dashboard_data(half, as_of_date)
    by_team = group_by(result, "ops_team")
    by_company = group_by(result, "company")

    # 필터 적용
    details = result["details"]
    if team:
        details = [d for d in details if d["ops_team"] == team]
    if status == "done":
        details = [d for d in details if d["completed"]]
    elif status == "pending":
        details = [d for d in details if not d["completed"]]

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "result": result,
        "details": details,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "by_company": dict(sorted(by_company.items())),
        "half": half,
        "half_label": "상반기" if half == "H1" else "하반기",
        "as_of": as_of_date,
        "next_week": as_of_date + timedelta(days=7),
        "today": date.today(),
        "filter_team": team or "",
        "filter_status": status or "",
        "teams": sorted(by_team.keys()),
        "jira_error": jira_error,
    })


@app.post("/send-report")
def trigger_report(half: str = Form(...)):
    """수동 Teams 리포트 발송"""
    send_report(half=half)
    return RedirectResponse(url=f"/?half={half}", status_code=303)


@app.get("/api/summary")
def api_summary(half: str | None = None, as_of: str | None = None):
    """JSON API (외부 연동용)"""
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


@app.get("/health")
def health():
    return {"status": "ok"}
