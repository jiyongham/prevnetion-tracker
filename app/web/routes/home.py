# app/web/routes/home.py
"""
포털 홈 - DR훈련/용량관리/EoS 중 어디로 들어갈지 고르는 진입 화면.

기존에는 "/"가 DR 대시보드였는데(DR/용량관리/EoS 세 영역이 생기면서 어느 하나가
루트를 차지할 이유가 없어짐), 이제 "/"는 이 선택 화면이 차지하고 DR 대시보드는
"/dr"로 옮겼다 (용량관리 "/capacity", EoS "/eos"와 형태를 맞춤).

각 모듈 카드의 완료율은 새로 계산하지 않고 각 도메인이 이미 쓰는 완료율 계산 함수를
그대로 재사용한다 (DR/EoS는 이미 있는 캐시를 그대로 타 비용이 거의 없고, 용량관리는
자체 대시보드와 동일하게 매 요청 JIRA 조회 - 대시보드보다 더 나빠지지 않는다). 외부
조회가 실패해도 카드 하나가 "확인 필요"로 빠질 뿐 포털 진입 자체는 항상 뜨게 한다.
"""
import logging
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.services import dr_data, eos_data
from app.services.completion import calc_completion
from app.services.eos import calc_eos_completion
from app.services.report import get_current_half
from app.web.deps import templates
from app.web.routes.capacity import get_capacity_dashboard_data

logger = logging.getLogger(__name__)

router = APIRouter()


def _dr_summary(today: date) -> tuple[int, int, str | None]:
    half = get_current_half()
    items = dr_data.load_items(half)
    ticket_map, jira_error = dr_data.get_ticket_map(half, items)
    result = calc_completion(items, ticket_map, today)
    return result["done"], result["total"], jira_error


def _capacity_summary(today: date) -> tuple[int, int, str | None]:
    done = total = 0
    error = None
    for sheet in ("DATA", "ARCH"):
        result, jira_error = get_capacity_dashboard_data(sheet, today)
        done += result["done"]
        total += result["total"]
        error = error or jira_error
    return done, total, error


def _eos_summary(today: date) -> tuple[int, int, str | None]:
    items, ticket_map, polestar_confirmed, jira_error = eos_data.get_eos_data()
    result = calc_eos_completion(items, ticket_map, today, polestar_confirmed=polestar_confirmed)
    return result["done"], result["total"], jira_error


_MODULES = (
    {
        "code": "DR", "label": "DR 모의훈련", "href": "/dr",
        "desc": "재해복구 대상의 실전환·무중단 수행률을 추적합니다.",
        "summarize": _dr_summary,
    },
    {
        "code": "CAP", "label": "용량관리", "href": "/capacity",
        "desc": "ASM·파일시스템 디스크 증설 대상의 진행 현황을 관리합니다.",
        "summarize": _capacity_summary,
    },
    {
        "code": "EOS", "label": "EoS 전환", "href": "/eos",
        "desc": "노후 OS·DB 시스템의 전환 완료 여부를 점검합니다.",
        "summarize": _eos_summary,
    },
)


@router.get("/", response_class=HTMLResponse)
def portal_home(request: Request):
    today = date.today()

    modules = []
    for m in _MODULES:
        try:
            done, total, error = m["summarize"](today)
        except Exception as e:
            logger.warning(f"포털 홈 {m['code']} 요약 실패: {e}")
            done, total, error = 0, 0, str(e)

        pct = round(done / total * 100) if total else None
        modules.append({
            "code": m["code"],
            "label": m["label"],
            "href": m["href"],
            "desc": m["desc"],
            "done": done,
            "total": total,
            "pct": pct,
            # 신호등 배색: 80%+ 정상, 50~79% 진행중, 그 아래 주의 (개별 화면 배지의
            # ==100/그외 2단계 기준과 달리 포털 카드는 3단계로 더 세분화해서 보여준다)
            "band": "ok" if pct is not None and pct >= 80 else "mid" if pct is not None and pct >= 50 else "bad",
            # 세그먼트 바(20칸) 중 채울 칸 수
            "bar_filled": round(pct / 5) if pct is not None else 0,
            "error": error,
        })

    return templates.TemplateResponse("portal.html", {
        "request": request,
        "modules": modules,
        "as_of": today,
    })
