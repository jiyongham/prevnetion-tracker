# app/services/kernel.py
"""
OS 커널 패치 진척 판정.

완료 근거가 다른 도메인과 다르다. 이 엑셀엔 완료 컬럼도, 패치 전 커널 버전도 없어서
"올라갔는지"를 비교할 기준이 엑셀 안에 없다. 그래서 두 축으로 판정한다.

1) 관리자가 화면에서 수동 완료 체크        → "완료표기"
2) 외부에서 확인된 '패치 완료 호스트' 목록  → "패치 확인"

2)의 공급원은 아직 확정 전이다. Polestar REST에는 OS 패치 레벨 필드가 아예 없고
(인터페이스 정의서 108개 엔드포인트 전수 확인), 화면(PQL/상세)에만 있는 값이라
경로가 정해지면 patched_hosts 집합만 만들어 넘기면 되도록 판정에서 분리해 두었다.

상태 5분류(완료/지연/예정/대략/미계획)와 정렬 규칙은 DR훈련·EoS와 동일하게 맞춘다 -
화면을 오가는 사람이 도메인마다 다른 규칙을 외우지 않아도 되게.
"""
from datetime import date

from app.core.date_utils import parse_schedule
from app.core.kernel_loader import get_targets
from app.services.completion import DONE_MARKS, approx_schedule


def normalize_kernel(value: str) -> str:
    """
    커널 버전 표기 정규화. Polestar는 '4.18.0-553.22.1.el8_10.x86_64' 형태로 주는데
    공백/대소문자만 흔들리므로 그 정도만 흡수한다 (버전 비교는 하지 않는다 -
    배포판마다 체계가 달라 문자열 비교가 오히려 안전하다).
    """
    return (value or "").strip().lower()


def judge_kernel(
    item: dict,
    patched_hosts: dict[str, str] | set[str] | None = None,
) -> tuple[bool, str]:
    """
    한 대상의 패치 완료 여부. 반환: (완료여부, 근거)

    patched_hosts: 외부에서 '패치가 확인된' 호스트명 집합 또는 {호스트명: 커널버전}.
    None이면 아직 그 근거를 조회하지 못한 상태이므로 수동 표기만으로 판정한다.
    """
    if (item.get("excel_done") or "").upper() in DONE_MARKS:
        return True, "완료표기"

    host = (item.get("hostname") or "").strip().lower()
    if patched_hosts and host in patched_hosts:
        detail = patched_hosts[host] if isinstance(patched_hosts, dict) else ""
        return True, f"패치 확인 ({detail})" if detail else "패치 확인"

    return False, ""


def calc_kernel_completion(
    items: list[dict],
    as_of: date,
    base_year: int | None = None,
    patched_hosts: dict[str, str] | set[str] | None = None,
) -> dict:
    """완료율 계산 (제외되지 않은 대상이 분모)"""
    year = base_year or as_of.year
    targets = get_targets(items)

    done = 0
    details = []

    for item in targets:
        completed, reason = judge_kernel(item, patched_hosts)
        if completed:
            done += 1

        sched = parse_schedule(item.get("schedule_raw", ""), year)
        overdue_unfulfilled = bool(sched) and sched < as_of and not completed
        planned = bool(sched) and not overdue_unfulfilled

        if completed:
            status_label = "완료"
        elif not sched:
            status_label = "대략" if (item.get("schedule_raw") or "").strip() else "미계획"
        elif overdue_unfulfilled:
            status_label = "지연"
        else:
            status_label = "예정"

        sort_date = sched or (
            approx_schedule(item.get("schedule_raw"), year) if status_label == "대략" else None
        )

        details.append({
            "item_no": item["item_no"],
            "no": item["no"],
            "insight_key": item.get("insight_key", ""),
            "scope": item.get("scope", ""),
            "system_name": item["system_name"],
            "hostname": item["hostname"],
            "ip": item["ip"],
            "ops_team": item["ops_team"],
            "owner": item["owner"],
            "company": item.get("company", ""),
            "center": item.get("center", ""),
            "os": item.get("os", ""),
            "db": item.get("db", ""),
            "infra_type": item.get("infra_type", ""),
            "server_part": item.get("server_part", ""),
            "schedule_raw": item.get("schedule_raw", ""),
            "schedule": sched,
            "schedule_sort": sort_date,
            "schedule_disp": sched.strftime("%Y-%m-%d") if sched else (item.get("schedule_raw") or ""),
            "planned": planned,
            "status_label": status_label,
            "completed": completed,
            "reason": reason,
            "evidence": item.get("evidence", ""),
            "note": item.get("note", ""),
            "input_source": item.get("input_source", "excel"),
            "updated_by": item.get("updated_by", ""),
            "updated_at": item.get("updated_at", ""),
        })

    total = len(targets)
    return {
        "as_of": as_of,
        "total": total,
        "done": done,
        "rate": round(done / total * 100, 1) if total else 0.0,
        "no_schedule": len([d for d in details if not d["planned"]]),
        "details": details,
    }
