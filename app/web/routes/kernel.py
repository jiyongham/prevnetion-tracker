# app/web/routes/kernel.py
"""
OS 커널 패치(보안 취약점 CVSS 8~9점대) 진척 라우트.

다른 도메인과 다른 점이 둘 있다.

1) 계획이 엑셀에 없다. 이 엑셀은 자산 목록만 오고 조치계획·완료 컬럼이 아예 없어서
   계획은 전부 이 화면에서 취합한다 (kernel_input 테이블).
2) 완료 근거가 아직 자동화되지 않았다. Polestar REST에 OS 패치 레벨 필드가 없고,
   화면 PQL은 거르기만 되고 값을 안 준다. 게다가 타겟 커널이 확정되기 전에는
   PQL 결과가 '패치된 서버'가 아니라 '그 커널을 쓰는 배포판'을 집어낸다
   (자세한 경위는 config.kernel_patched_export_path 주석). 그래서 지금은 관리자의
   수동 완료 체크만으로 판정하고, 근거 파일이 준비되면 경로만 채우면 자동으로 합쳐진다.

개발기/운영기는 별도 파일로 오므로 scope 탭으로 가른다. 운영기 파일이 없으면 그 탭은
아예 나오지 않는다 - 빈 화면으로 들어가 "왜 0건이지" 하는 일이 없게.
"""
import io
import logging
from datetime import date
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app.config import settings
from app.core.kernel_loader import SCOPE_LABELS, available_scopes, load_kernel_items_merged
from app.models.db import get_kernel_input, upsert_kernel_input
from app.services.completion import group_by
from app.services.kernel import calc_kernel_completion
from app.services.kernel_patched import patched_hosts_for, read_patched_export
from app.web.deps import require_updated_by, resolve_owner, templates

logger = logging.getLogger(__name__)

router = APIRouter()


def resolve_scope(scope: str | None) -> str:
    """요청된 범위를 실제 파일이 있는 범위로 좁힌다. 없으면 첫 번째(보통 개발기)."""
    scopes = available_scopes()
    if not scopes:
        return "dev"
    return scope if scope in scopes else scopes[0]


def get_kernel_dashboard_data(scope: str, as_of: date):
    """반환: (완료율 집계, 병합된 전체 항목, 근거 파일 정보)

    items를 같이 돌려주는 이유는 EoS와 같다 - 호출부가 제외 목록을 만들려고 엑셀을
    다시 읽으면 그 사이 파일이 갱신될 경우 집계와 목록의 기준이 어긋난다.
    """
    items = load_kernel_items_merged(scope=scope)
    export = read_patched_export()
    patched = patched_hosts_for(items, export) if export["rows"] else set()
    result = calc_kernel_completion(items, as_of, patched_hosts=patched)
    return result, items, export


@router.get("/kernel", response_class=HTMLResponse)
def kernel_dashboard(
    request: Request,
    scope: str | None = None,
    team: str | None = None,
    status: str | None = None,
    q: str | None = None,
):
    today = date.today()
    scope = resolve_scope(scope)

    result, all_items, export = get_kernel_dashboard_data(scope, today)
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
                or kw in (d["os"] or "").lower()
            )

        details = [d for d in details if _match(d)]

    # 계획 없는 대상이 뒤로 가되, 그 안에서는 OS별로 묶여 보이게 한다 -
    # 패치는 배포판 단위로 묶어 돌리는 일이 많아 담당자가 그 순서로 훑는다.
    details = sorted(
        details,
        key=lambda d: (d["schedule_sort"] is None, d["schedule_sort"] or date.max, d["os"], d["system_name"]),
    )

    web_excluded = [i for i in all_items if i.get("is_excluded")]

    # OS 버전별 분포. 타겟 커널이 배포판마다 달라 "어느 버전이 몇 대인지"가
    # 계획 수립의 첫 질문이 된다 (진척률만으로는 안 보이는 정보).
    by_os = group_by(result, "os")

    return templates.TemplateResponse("kernel.html", {
        "request": request,
        "result": result,
        "details": details,
        "web_excluded": web_excluded,
        "by_team": dict(sorted(by_team.items(), key=lambda x: x[1]["rate"])),
        "by_os": dict(sorted(by_os.items(), key=lambda x: -x[1]["total"])),
        "as_of": today,
        "scope": scope,
        "scopes": [(s, SCOPE_LABELS[s]) for s in available_scopes()],
        "scope_label": SCOPE_LABELS.get(scope, scope),
        "filter_team": team or "",
        "filter_status": status or "",
        "q": q or "",
        "admins": sorted(settings.kernel_admin_set),
        "patched_source": export["source"],
        "patched_rows": export["rows"],
    })


def _resolve_kernel_is_done(item_no: str, requested: bool, updated_by: str) -> bool:
    """완료 처리는 관리자만. 지금은 이게 유일한 완료 근거라 더 엄격히 지킨다."""
    if updated_by in settings.kernel_admin_set:
        return requested
    existing = get_kernel_input(item_no)
    return bool(existing["is_done"]) if existing else False


@router.post("/api/kernel/save")
async def api_kernel_save(request: Request):
    """인라인 저장 (한 행)"""
    data = await request.json()
    item_no = data["item_no"]
    updated_by = require_updated_by(data.get("updated_by", ""))
    upsert_kernel_input(
        item_no=item_no,
        schedule=(data.get("schedule") or "").strip(),
        is_done=_resolve_kernel_is_done(item_no, bool(data.get("is_done")), updated_by),
        evidence=(data.get("evidence") or "").strip(),
        note=(data.get("note") or "").strip(),
        owner=resolve_owner(data.get("owner", ""), updated_by, settings.kernel_admin_set),
        updated_by=updated_by,
    )
    return JSONResponse({"ok": True})


@router.post("/api/kernel/bulk-save")
async def api_kernel_bulk_save(request: Request):
    """변경된 행만 일괄 저장. 계획을 웹에서만 받기 때문에 이 화면에선 특히 많이 쓰인다."""
    data = await request.json()
    updated_by = require_updated_by(data.get("updated_by", ""))
    rows = data.get("rows", [])

    for r in rows:
        item_no = r["item_no"]
        upsert_kernel_input(
            item_no=item_no,
            schedule=(r.get("schedule") or "").strip(),
            is_done=_resolve_kernel_is_done(item_no, bool(r.get("is_done")), updated_by),
            evidence=(r.get("evidence") or "").strip(),
            note=(r.get("note") or "").strip(),
            updated_by=updated_by,
        )
    return JSONResponse({"ok": True, "count": len(rows)})


@router.post("/api/kernel/exclude")
async def api_kernel_exclude(request: Request):
    """대상 제외/해제. 완료율 분모가 바뀌는 처리라 사유 없이는 받지 않는다."""
    data = await request.json()
    item_no = (data.get("item_no") or "").strip()
    updated_by = (data.get("updated_by") or "").strip()
    excluded = bool(data.get("excluded", True))
    reason = (data.get("reason") or "").strip()

    if not item_no:
        return JSONResponse({"ok": False, "error": "필수 값이 없습니다."}, status_code=400)
    if updated_by not in settings.kernel_admin_set:
        return JSONResponse(
            {"ok": False, "error": "제외 처리는 관리자만 가능합니다."}, status_code=403
        )
    if excluded and not reason:
        return JSONResponse({"ok": False, "error": "제외 사유를 입력해주세요."}, status_code=400)

    existing = get_kernel_input(item_no) or {}
    upsert_kernel_input(
        item_no=item_no,
        schedule=existing.get("schedule") or "",
        is_done=bool(existing.get("is_done")),
        evidence=existing.get("evidence") or "",
        note=existing.get("note") or "",
        updated_by=updated_by,
        is_excluded=excluded,
        exclude_reason=reason if excluded else "",
    )
    return JSONResponse({"ok": True})


@router.get("/kernel/export")
def export_kernel_excel(scope: str | None = None):
    """현재 범위의 대상 목록을 엑셀로. 화면에서 보는 값 그대로 담는다."""
    scope = resolve_scope(scope)
    as_of_date = date.today()
    result, _, _ = get_kernel_dashboard_data(scope, as_of_date)

    rows = [{
        "Insight Key": d["insight_key"],
        "시스템명": d["system_name"],
        "호스트명": d["hostname"],
        "IP": d["ip"],
        "운영팀": d["ops_team"],
        "담당자": d["owner"],
        "OS": d.get("os", ""),
        "센터": d.get("center", ""),
        "자산구분": d.get("company", ""),
        "조치계획": d["schedule_disp"],
        "상태": d["status_label"],
        "완료": "O" if d["completed"] else "",
        "판정근거": d.get("reason", ""),
        "증적": d.get("evidence", ""),
        "비고": d.get("note", ""),
    } for d in result["details"]]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name=f"커널패치_{scope}")
    buf.seek(0)

    label = SCOPE_LABELS.get(scope, scope)
    fname = f"OS커널패치_진척_{label}_{as_of_date}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="kernel_export_{scope}_{as_of_date}.xlsx"; '
            f"filename*=UTF-8''{quote(fname)}"
        },
    )


@router.get("/kernel/api/summary")
def api_kernel_summary(scope: str | None = None):
    scope = resolve_scope(scope)
    result, _, export = get_kernel_dashboard_data(scope, date.today())
    return {
        "scope": scope,
        "scope_label": SCOPE_LABELS.get(scope, scope),
        "as_of": str(result["as_of"]),
        "total": result["total"],
        "done": result["done"],
        "rate": result["rate"],
        "no_schedule": result["no_schedule"],
        # 완료 근거 파일이 붙었는지. 비어 있으면 수동 체크만으로 나온 숫자다.
        "patched_source": export["source"],
        "by_team": group_by(result, "ops_team"),
        "by_os": group_by(result, "os"),
    }
