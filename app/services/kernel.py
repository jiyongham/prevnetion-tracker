# app/services/kernel.py
"""
OS 커널 패치 진척 판정.

완료는 두 축으로 판정한다.

1) 엑셀 '완료여부' 또는 화면 수동 체크          → "완료표기"
2) Polestar OS 패치 레벨이 타겟 커널 이상       → "패치 확인"

2)의 값은 Polestar PQL 결과 내보내기에서 온다(kernel_patched). 판정이 '목록에 있나'가
아니라 '버전이 타겟에 닿았나'인 이유: 임시로 써 본 `config[OS 패치 레벨]="4.18.0-553%"`
결과는 그 커널을 쓰는 RHEL 8.10 서버가 통째로 걸려 '패치 완료'가 아니라 '원래 그 버전'을
집어냈다. 타겟도 OS 계열마다 다르다(8.x=4.18 / 9.x=5.14) - settings.kernel_target_map.

상태 5분류(완료/지연/예정/대략/미계획)와 정렬 규칙은 DR훈련·EoS와 동일하게 맞춘다 -
화면을 오가는 사람이 도메인마다 다른 규칙을 외우지 않아도 되게.
"""
import re
from datetime import date

from app.config import settings
from app.core.date_utils import parse_schedule
from app.core.kernel_loader import get_targets
from app.services.completion import DONE_MARKS, approx_schedule
from app.services.kernel_patched import patch_levels_for


# '4.18.0-553.164.1.el8_10' -> (4.18.0, 553.164.1, el8_10)
_KERNEL_RE = re.compile(r"(\d+(?:\.\d+)*)-(\d+(?:\.\d+)*)\.(el\d+(?:_\d+)?)")
_ARCH_SUFFIX_RE = re.compile(r"\.(x86_64|aarch64|noarch|i686)$", re.I)
# 'Linux_Redhat9.7' / 'RHEL 8.10' -> 메이저 '9' / '8'
_OS_MAJOR_RE = re.compile(r"(?:redhat|rhel|centos|rocky)\D*(\d+)", re.I)


def normalize_kernel(value: str) -> str:
    """
    커널 버전 표기 정규화.
    엑셀 타겟은 'kernel-4.18.0-553.164.1.el8_10', Polestar는
    '4.18.0-553.22.1.el8_10.x86_64' 형태로 와서 접두어/아키텍처 꼬리만 다르다.
    """
    v = (value or "").strip().lower()
    v = re.sub(r"^kernel-", "", v)
    return _ARCH_SUFFIX_RE.sub("", v)


def parse_os_major(os_text: str) -> str:
    """'Linux_Redhat9.7' -> '9'. 못 읽으면 빈 문자열."""
    m = _OS_MAJOR_RE.search(os_text or "")
    return m.group(1) if m else ""


def target_kernel_for(item: dict) -> str:
    """이 서버의 OS 계열에 맞는 타겟 커널. 타겟이 없으면 빈 문자열."""
    return settings.kernel_target_map.get(parse_os_major(item.get("os", "")), "")


def meets_target(level: str, target: str) -> bool:
    """
    현재 커널이 타겟 이상인지.

    같은 el 계열(el8_10 / el9_8)일 때만 숫자로 비교한다. 계열이 다르면 비교 자체가
    의미가 없고(4.18 vs 5.14), 못 읽는 표기면 억지로 판정하지 않는다 - 완료는
    부풀면 되돌리기 어려운 숫자라 애매하면 미완료로 둔다.
    """
    ln, tn = normalize_kernel(level), normalize_kernel(target)
    if not ln or not tn:
        return False
    if ln == tn:
        return True

    lm, tm = _KERNEL_RE.search(ln), _KERNEL_RE.search(tn)
    if not lm or not tm or lm.group(3) != tm.group(3):
        return False

    def parts(m):
        return (
            tuple(int(x) for x in m.group(1).split(".")),
            tuple(int(x) for x in m.group(2).split(".")),
        )

    return parts(lm) >= parts(tm)


def judge_kernel(
    item: dict,
    patch_levels: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """
    한 대상의 패치 완료 여부. 반환: (완료여부, 근거)

    patch_levels: {호스트명(소문자): OS 패치 레벨}. 키가 없으면 Polestar에서 확인되지
    않은 서버이고, 키는 있는데 값이 비면 '패치 목록엔 있으나 값을 모르는' 예전 형식
    파일이다 - 그때는 예전처럼 목록에 있는 것만으로 완료로 본다.
    """
    if (item.get("excel_done") or "").upper() in DONE_MARKS:
        return True, "완료표기"

    host = (item.get("hostname") or "").strip().lower()
    if not patch_levels or host not in patch_levels:
        return False, ""

    level = patch_levels[host]
    if not level:
        return True, "패치 확인 (목록 기준)"

    target = target_kernel_for(item)
    if not target:
        # OS를 못 읽었거나 그 계열 타겟이 설정에 없다. 값을 보여주고 사람이 판단하게 둔다.
        return False, f"타겟 미설정 (OS {item.get('os') or '불명'}, 현재 {level})"
    if meets_target(level, target):
        return True, f"패치 확인 ({level})"
    return False, f"미달 (현재 {level} / 타겟 {normalize_kernel(target)})"


def calc_kernel_completion(
    items: list[dict],
    as_of: date,
    base_year: int | None = None,
    patch_levels: dict[str, str] | None = None,
) -> dict:
    """
    완료율 계산 (제외되지 않은 대상이 분모).

    patch_levels를 안 주면 여기서 직접 읽는다. 포털 홈·팀별 조회는 이 함수만 부르는데,
    예전엔 근거를 안 넘겨서 같은 대상이 화면마다 다른 완료 수로 보였다.
    """
    if patch_levels is None:
        patch_levels = patch_levels_for(items)

    year = base_year or as_of.year
    targets = get_targets(items)

    done = 0
    details = []

    for item in targets:
        completed, reason = judge_kernel(item, patch_levels)
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
            "os_major": parse_os_major(item.get("os", "")),
            # 화면에서 "왜 완료/미완료인지"를 숫자로 보여주기 위한 값
            "patch_level": (patch_levels or {}).get(
                (item.get("hostname") or "").strip().lower(), ""
            ),
            "target_kernel": normalize_kernel(target_kernel_for(item)),
            "work_time": item.get("work_time", ""),
            "safer": item.get("safer", ""),
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
            "last_inputter": item.get("last_inputter", ""),
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
