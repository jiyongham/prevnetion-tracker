# app/web/routes/eos.py
"""EoS(노후 OS/DB 전환 - [예방1]) 관련 라우트"""
import logging
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import eos_sender, settings
from app.core.date_utils import week_ranges
from app.core.eos_loader import load_eos_items_merged
from app.core.teams_client import send_teams_dm
from app.models.db import (
    add_eos_next_week_plan,
    delete_eos_polestar_seen,
    get_eos_input,
    get_eos_next_week_plan,
    get_eos_remind_log_summary,
    log_eos_remind,
    remove_eos_next_week_plan,
    upsert_eos_input,
)
from app.services.completion import group_by
from app.services.eos import build_no_reply_details, calc_eos_completion, filter_track
from app.services import eos_chatbot, evidence_check, last_report
from app.services.eos_data import (
    cached_at,
    get_eos_data,
    polestar_error,
    invalidate_cache as invalidate_eos_cache,
    prewarm as prewarm_eos,
)
from app.services.eos_plan_chat import build_candidates, parse_plan_message
from app.services.eos_reminder import group_eos_no_reply, group_eos_unplanned
from app.services.eos_report import send_eos_report
from app.services.owner_check import collect_eos_targets_with_tickets, find_owner_mismatches
from app.web.deps import require_updated_by, resolve_owner, templates

logger = logging.getLogger(__name__)

router = APIRouter()


def external_as_of() -> str:
    """외부 데이터(JIRA/Polestar) 기준 시각 표시용. 갱신은 뒤에서 돌기 때문에
    화면 숫자가 몇 분 전 기준일 수 있어 언제 것인지 같이 보여준다."""
    at = cached_at()
    return datetime.fromtimestamp(at).strftime("%m-%d %H:%M") if at else ""


def get_eos_dashboard_data(as_of: date, use_jira: bool = True, track: str = "ALL"):
    """반환: (완료율 집계, 엑셀+DB 병합 전체 항목, JIRA 오류메시지)

    items를 같이 돌려주는 이유: 호출부가 제외/미응답 목록을 만들려고 엑셀을 또 읽고 있었는데,
    같은 요청 안에서 두 번 읽으면 그 사이 엑셀이 갱신될 경우 집계와 목록의 기준이 어긋난다.
    """
    items, ticket_map, polestar_confirmed, jira_error = get_eos_data(use_external=use_jira)
    result = calc_eos_completion(
        filter_track(items, track), ticket_map, as_of, polestar_confirmed=polestar_confirmed
    )
    return result, items, jira_error


@router.get("/eos", response_class=HTMLResponse)
def eos_dashboard(
    request: Request,
    track: str = "ALL",
    team: str | None = None,
    status: str | None = None,
    q: str | None = None,
    report_warning: str | None = None,
    sent: str | None = None,
):
    if track not in ("ALL", "OS", "DB"):
        track = "ALL"
    today = date.today()

    result, all_items, jira_error = get_eos_dashboard_data(today, track=track)
    by_team = group_by(result, "ops_team")

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

    # 증적란에 적힌 JIRA 키의 실제 상태(반려/미종결)를 표시용으로 붙인다
    evidence_check.annotate(details)
    evidence_warn_cnt = sum(1 for d in details if d.get("evidence_level"))

    # 관리자가 웹에서 제외 처리한 대상 (엑셀 원본 제외와 구분해 사유·처리자를 보여준다)
    web_excluded = [
        i for i in all_items
        if i["status"] == "excluded" and i.get("input_source") == "web"
    ]

    return templates.TemplateResponse("eos.html", {
        "request": request,
        "result": result,
        "details": details,
        "excluded_cnt": excluded_cnt,
        "evidence_warn_cnt": evidence_warn_cnt,
        "web_excluded": web_excluded,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "as_of": today,
        "track": track,
        "filter_team": team or "",
        "filter_status": status or "",
        "q": q or "",
        "teams": sorted(by_team.keys()),
        "admins": sorted(settings.eos_admin_set),
        "jira_error": jira_error,
        "polestar_error": polestar_error(),
        "report_warning": report_warning,
        # 발송 직후에만(sent=1) 방금 나간 본문을 화면에 띄운다
        "sent_report": last_report.get("eos") if sent else "",
        "jira_base": settings.jira_url.rstrip("/"),
        "data_as_of": external_as_of(),
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


@router.post("/api/eos/clear-polestar")
async def api_eos_clear_polestar(request: Request):
    """
    Polestar '_OLD' 관측 기록 삭제 (관리자만).
    한 번 확인된 '_OLD'는 CI가 폐기돼도 완료 근거로 계속 인정되므로, 오탐이었다고
    판단되면 이 기록을 지워야 판정에서 빠진다. 다음 조회에서 다시 확인되면 재기록된다.
    """
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()

    if not item_no:
        return JSONResponse({"ok": False, "error": "대상이 지정되지 않았습니다."}, status_code=400)
    if updated_by not in settings.eos_admin_set:
        return JSONResponse(
            {"ok": False, "error": "관측 기록 삭제는 관리자만 가능합니다."}, status_code=403
        )

    delete_eos_polestar_seen(item_no)
    logger.info(f"Polestar 관측 기록 삭제: {item_no} (by {updated_by})")
    invalidate_eos_cache()
    prewarm_eos()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# EoS 미계획 리마인드 미리보기 (운영팀별 초안, 발송 없음)
# ─────────────────────────────────────────────
@router.get("/eos/remind-preview", response_class=HTMLResponse)
def eos_remind_preview(
    request: Request,
    team: str | None = None,
    kind: str = "blank",
):
    result, all_items, jira_error = get_eos_dashboard_data(date.today())

    blank_groups = group_eos_unplanned(result["details"], hinted=False)
    hinted_groups = group_eos_unplanned(result["details"], hinted=True)
    no_reply_groups = group_eos_no_reply(all_items)

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
        "sender_name": eos_sender(),
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
    # 발송은 하되, 지난주 대비 이상 징후가 있으면 화면에 띄워 확인하게 한다 (DR훈련과 동일)
    warning = send_eos_report()
    url = "/eos?sent=1"
    if warning:
        url += f"&report_warning={quote(warning)}"
    return RedirectResponse(url=url, status_code=303)


# ─────────────────────────────────────────────
# 차주 계획 챗봇 (JIRA/Confluence로 못 찾은 주에 관리자가 아는 대로 자유 텍스트 입력)
# ─────────────────────────────────────────────
@router.get("/eos/plan-chat", response_class=HTMLResponse)
def eos_plan_chat_page(request: Request):
    today = date.today()
    perf_start, perf_end, plan_start, plan_end = week_ranges(today)

    items = load_eos_items_merged()
    by_no = {i["item_no"]: i for i in items}

    def saved_rows(week_start: date, kind: str) -> list[dict]:
        saved = get_eos_next_week_plan(week_start.isoformat(), kind=kind)
        return [
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
        "plan_start": plan_start,
        "plan_end": plan_end,
        "perf_start": perf_start,
        "perf_end": perf_end,
        "plan_saved": saved_rows(plan_start, "plan"),
        "perf_saved": saved_rows(perf_start, "perf"),
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
    """확인된 후보들을 그 주 차주 계획(plan) 또는 금주 실적(perf) 대상으로 저장 (관리자만)"""
    data = await request.json()
    updated_by = require_updated_by(data.get("updated_by", ""))
    if updated_by not in settings.eos_admin_set:
        return JSONResponse(
            {"ok": False, "error": "계획·실적 입력은 관리자만 가능합니다."}, status_code=403
        )

    kind = (data.get("kind") or "plan").strip()
    item_nos = data.get("item_nos") or []
    week_start = (data.get("week_start") or "").strip()
    week_end = (data.get("week_end") or "").strip()
    if not item_nos or not week_start or not week_end:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)

    try:
        add_eos_next_week_plan(item_nos, week_start, week_end, updated_by, kind=kind)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "count": len(item_nos)})


@router.post("/api/eos/plan-chat/remove")
async def api_eos_plan_chat_remove(request: Request):
    """잘못 추가된 항목 제거 (관리자만)"""
    data = await request.json()
    updated_by = require_updated_by(data.get("updated_by", ""))
    if updated_by not in settings.eos_admin_set:
        return JSONResponse(
            {"ok": False, "error": "계획·실적 수정은 관리자만 가능합니다."}, status_code=403
        )

    kind = (data.get("kind") or "plan").strip()
    item_no = (data.get("item_no") or "").strip()
    week_start = (data.get("week_start") or "").strip()
    if not item_no or not week_start:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)

    try:
        remove_eos_next_week_plan(item_no, week_start, kind=kind)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# 진척 조회 챗봇 (사내 LLM Agent)
# ─────────────────────────────────────────────
@router.post("/api/eos/chat")
async def api_eos_chat(request: Request):
    data = await request.json()
    query = (data.get("query") or "").strip()
    name = (data.get("name") or "").strip()

    if not query:
        return JSONResponse({"ok": False, "error": "질문을 입력해주세요"}, status_code=400)

    try:
        reply = eos_chatbot.answer(name, query)
    except Exception as e:
        logger.warning(f"EoS 챗봇 실패: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "reply": reply})


# ─────────────────────────────────────────────
# 제외 처리 (관리자만) - 사유 필수
# ─────────────────────────────────────────────
@router.post("/api/eos/exclude")
async def api_eos_exclude(request: Request):
    """
    EoS 대상 제외/해제. 엑셀의 'EOS 진행/제외' 컬럼(착수 시점 판정)과 별개로,
    운영 중 확정된 제외를 사유와 함께 남긴다. 완료율 분모에서 빠지는 처리라
    사유 없이는 받지 않는다.
    """
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()
    excluded = bool(data.get("excluded", True))
    reason = (data.get("reason") or "").strip()

    if not item_no:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)
    if updated_by not in settings.eos_admin_set:
        return JSONResponse(
            {"ok": False, "error": "제외 처리는 관리자만 가능합니다."}, status_code=403
        )
    if excluded and not reason:
        return JSONResponse({"ok": False, "error": "제외 사유를 입력해주세요."}, status_code=400)

    existing = get_eos_input(item_no) or {}
    upsert_eos_input(
        item_no=item_no,
        schedule=existing.get("schedule") or "",
        is_done=bool(existing.get("is_done")),
        evidence=existing.get("evidence") or "",
        note=existing.get("note") or "",
        updated_by=updated_by,
        excluded=excluded,
        exclude_reason=reason if excluded else "",
    )
    # 대상 구성이 바뀌었으므로 티켓 매칭 캐시를 버리고, 곧바로 뒤에서 다시 채운다
    # (버리기만 하면 제외 직후 화면을 여는 사람이 재조회를 다 기다리게 된다)
    invalidate_eos_cache()
    prewarm_eos()
    return JSONResponse({"ok": True})
