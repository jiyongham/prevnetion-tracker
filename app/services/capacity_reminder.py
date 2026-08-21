# app/services/capacity_reminder.py
from app.config import settings
from app.services.reminder import (
    candidates_of,
    group_and_vote,
    has_schedule_hint,
    pick_representative,
)


def build_capacity_body(items: list[dict], show_raw: bool = False) -> str:
    """대상 목록 본문 (CI명 / 호스트명 / IP). show_raw면 현재 등록된 텍스트도 같이 표기"""
    lines = []
    for d in items:
        line = f"{d.get('ci_name', '')} / {d.get('hostname', '')} / {d.get('ip', '')}"
        if show_raw and d.get("schedule_raw"):
            line += f" (현재 등록: {d['schedule_raw']})"
        lines.append(line)
    return "\n\n".join(lines) if lines else "(대상 없음)"


def capacity_greeting_suffix(items: list[dict], hinted: bool = False) -> str:
    """인사말 뒤에 붙는 고정 문구 + 대상 목록 (DR훈련 greeting_suffix와 동일한 구조)"""
    if hinted:
        ask = (
            "다름아니라 공지드린 용량관리(디스크 증설) 대상 중 대략적인 일정만 등록되어 있어, "
            "정확한 날짜로 확정해서 알려주실 수 있을까요?"
        )
    else:
        ask = "다름아니라 공지드린 용량관리(디스크 증설) 대상의 계획된 일정 알 수 있을까요?"
    return (
        f"님. {settings.sender_team} {settings.capacity_sender_name}입니다.\n\n\n"
        f"{ask}\n\n"
        f"{build_capacity_body(items, show_raw=hinted)}\n\n\n"
        f"용량관리 진척 현황({settings.dashboard_url}/capacity)에 기입 요청드립니다."
    )


def build_capacity_message(given: str, items: list[dict], hinted: bool = False) -> str:
    """담당자 1인에게 보낼 미계획 리마인드 초안 (전체 문자열)"""
    return f"안녕하세요, {given}{capacity_greeting_suffix(items, hinted)}"


def no_reply_greeting_suffix(items: list[dict]) -> str:
    """미회신(증설 여부 O/X 미기재) 대상 리마인드 인사말"""
    ask = "다름아니라 공지드린 용량관리(디스크 증설) 대상 중 증설 필요 여부(O,X) 응답이 아직 없어 확인 요청드립니다."
    return (
        f"님. {settings.sender_team} {settings.capacity_sender_name}입니다.\n\n\n"
        f"{ask}\n\n"
        f"{build_capacity_body(items)}\n\n\n"
        f"용량관리 진척 현황({settings.dashboard_url}/capacity)에 증설 필요 여부(O,X) 기입 요청드립니다."
    )


def build_no_reply_message(given: str, items: list[dict]) -> str:
    """담당자 1인에게 보낼 미회신 리마인드 초안 (전체 문자열)"""
    return f"안녕하세요, {given}{no_reply_greeting_suffix(items)}"


def group_capacity_no_reply(items: list[dict]) -> list[dict]:
    """
    증설 여부(O,X)가 공란인 '미회신' 대상을 시스템 운영팀 단위로 묶고 초안까지 생성.
    주의: items는 대상(O)만이 아니라 엑셀 전체 행이어야 한다 (calc_capacity_completion
    결과의 details는 이미 O만 걸러진 상태라 여기엔 못 씀 - load_capacity_items_merged를 그대로 넘길 것).

    반환: [{owner, ops_team, name, team, given, count, targets, message}, ...] (대상 많은 순)
    """
    raw_groups = group_and_vote(
        items,
        key_fn=lambda d: d.get("ops_team"),
        include_fn=lambda d: (d.get("expand_flag") or "") not in ("O", "X"),
    )

    result = []
    for g in raw_groups:
        top_raw, p, given = pick_representative(g)
        result.append({
            "owner": top_raw,
            "ops_team": g["key"],
            "name": p["name"],
            "team": p["team"],
            "given": given,
            "count": len(g["items"]),
            "targets": g["items"],
            "greeting_suffix": no_reply_greeting_suffix(g["items"]),
            "candidates": candidates_of(g["items"], None),
            "message": build_no_reply_message(given, g["items"]),
        })

    return sorted(result, key=lambda x: (-x["count"], x["ops_team"]))


def group_capacity_unplanned(details: list[dict], hinted: bool | None = None) -> list[dict]:
    """
    미계획(증설 일정 미등록) 대상을 '시스템 운영팀' 단위로 묶고 초안까지 생성.
    DR훈련의 group_unplanned_by_service와 동일한 방식(대표 담당자 다수결 선정)이지만,
    용량관리 엑셀엔 '서비스'(주업무명) 컬럼이 없어 시스템 운영팀 단위로 묶는다.

    hinted 필터:
    - None: 전체 미계획
    - False: 일정칸이 완전히 빈 대상만
    - True : '11월 예정'처럼 텍스트는 있지만 날짜로 파싱 안 된 대상만

    반환: [{owner, ops_team, name, team, given, count, targets, message}, ...] (대상 많은 순)
    """
    raw_groups = group_and_vote(
        details,
        key_fn=lambda d: d.get("ops_team"),
        include_fn=lambda d: not d.get("planned") and (hinted is None or has_schedule_hint(d) == hinted),
    )

    is_hinted = bool(hinted)
    result = []
    for g in raw_groups:
        top_raw, p, given = pick_representative(g)
        result.append({
            "owner": top_raw,
            "ops_team": g["key"],
            "name": p["name"],
            "team": p["team"],
            "given": given,
            "count": len(g["items"]),
            "targets": g["items"],
            "greeting_suffix": capacity_greeting_suffix(g["items"], is_hinted),
            "candidates": candidates_of(g["items"], None),
            "message": build_capacity_message(given, g["items"], is_hinted),
        })

    return sorted(result, key=lambda x: (-x["count"], x["ops_team"]))
