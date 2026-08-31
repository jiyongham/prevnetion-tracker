# app/services/reminder.py
import re
from datetime import date, timedelta

from app.config import settings

# 두 글자 성 (성 제외 시 오인 방지용)
TWO_CHAR_SURNAMES = {"남궁", "선우", "황보", "제갈", "사공", "서문", "독고", "동방"}


def parse_owners(cell: str) -> list[dict]:
    """
    담당자 셀 파싱.
    '강대원-라이브쇼핑팀||김아름-커머스사업2그룹' 형태 (한 대상에 여러 명).
    반환: [{raw, name, team}, ...]
    """
    out = []
    for tok in (cell or "").split("||"):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            name, team = tok.split("-", 1)
        else:
            name, team = tok, ""
        out.append({"raw": tok, "name": name.strip(), "team": team.strip()})
    return out


def clean_name(name: str) -> str:
    """괄호 표기 제거: '홍길동(협력사명)' -> '홍길동'"""
    return re.sub(r"\(.*?\)", "", name or "").strip()


def strip_surname(name: str) -> str:
    """
    이름에서 성 제외 (홍길동 -> 길동).
    두 글자 성(남궁/선우 등)은 두 글자, 그 외에는 한 글자 제거.
    """
    name = clean_name(name)
    if len(name) <= 1:
        return name
    if name[:2] in TWO_CHAR_SURNAMES:
        return name[2:]
    return name[1:]


def has_schedule_hint(item: dict) -> bool:
    """일정칸이 완전히 비어있지 않고 '11월 예정'처럼 텍스트라도 적혀있는지"""
    return bool((item.get("schedule_raw") or "").strip())


def build_body(items: list[dict], show_raw: bool = False) -> str:
    """대상 목록 본문 (시스템명 / 호스트명 / IP). show_raw면 현재 등록된 텍스트도 같이 표기"""
    lines = []
    for d in items:
        line = f"{d.get('system_name', '')} / {d.get('hostname', '')} / {d.get('ip', '')}"
        if show_raw and d.get("schedule_raw"):
            # 예정 안내는 정규화된 M/D로, 미기입 재확인은 담당자가 적은 원문 그대로 보여준다
            if d.get("status_label") == "예정":
                line += f" (예정: {d.get('schedule_disp') or d['schedule_raw']})"
            else:
                line += f" (현재 등록: {d['schedule_raw']})"
        lines.append(line)
    return "\n\n".join(lines) if lines else "(대상 없음)"


def greeting_suffix(items: list[dict], kind: str = "blank") -> str:
    """
    인사말에서 '이름' 뒤에 붙는 고정 문구 + 대상 목록 (이름만 바꿔치기 가능하도록 분리).
    - blank   : 일정칸이 비어 있음 - 일정을 물어본다
    - hinted  : '11월 예정'처럼 대략적인 일정만 있음 - 정확한 날짜를 재요청한다
    - upcoming: 작업이 코앞 - 변경 티켓 발행을 미리 알린다 (독촉이 아니라 사전 안내)
    """
    if kind == "hinted":
        ask = "다름아니라 공지드린 하반기 DR 훈련 하기 대략적인 일정만 등록되어 있어, 정확한 날짜로 확정해서 알려주실 수 있을까요?"
        closing = f"DR 모의훈련 진척 현황({settings.dashboard_url})에 기입 요청드립니다."
    elif kind == "upcoming":
        ask = (
            f"다름아니라 하기 DR 훈련 작업 일정이 {settings.pre_work_remind_days}일 이내로 다가와 "
            "미리 안내드립니다. 변경 티켓 발행이 아직이시라면 사전 승인 기간을 고려해 준비 부탁드립니다."
        )
        closing = f"진행 상황은 DR 모의훈련 진척 현황({settings.dashboard_url})에서 확인하실 수 있습니다."
    else:
        ask = "다름아니라 공지드린 하반기 DR 훈련 하기 계획된 일정 알 수 있을까요?"
        closing = f"DR 모의훈련 진척 현황({settings.dashboard_url})에 기입 요청드립니다."
    return (
        f"님. {settings.sender_team} {settings.sender_name}입니다.\n\n\n"
        f"{ask}\n\n"
        f"{build_body(items, show_raw=kind != 'blank')}\n\n\n"
        f"{closing}"
    )


def build_message(given: str, items: list[dict], kind: str = "blank") -> str:
    """담당자 1인에게 보낼 리마인드 초안 (전체 문자열)"""
    return f"안녕하세요, {given}{greeting_suffix(items, kind)}"


def team_via_cmdb(name: str, items: list[dict], cmdb_map: dict | None, fallback: str) -> str:
    """
    CMDB(Insight) 조회 결과에서 동명이인을 이름으로 찾아 소속 팀을 보정한다.
    (엑셀 담당자 팀명이 조직변경으로 오래됐을 수 있어, DM 수신자 조회 정확도를 위해 사용)
    못 찾으면 엑셀 팀명을 그대로 둔다.
    """
    if not cmdb_map:
        return fallback
    target = clean_name(name)
    for d in items:
        asset = cmdb_map.get(d.get("no"))
        if not asset:
            continue
        for o in asset.get("owners", []):
            if clean_name(o["name"]) == target and o["team"]:
                return o["team"]
    return fallback


def candidates_of(items: list[dict], cmdb_map: dict | None = None) -> list[dict]:
    """대상들에 등장하는 담당자 후보 (중복 제거, 등장 순)"""
    seen: dict[str, dict] = {}
    for d in items:
        for p in parse_owners(d.get("owner", "")):
            if p["raw"] not in seen:
                team = team_via_cmdb(p["name"], items, cmdb_map, p["team"])
                seen[p["raw"]] = {
                    "raw": p["raw"],
                    "name": p["name"],
                    "team": team,
                    "given": strip_surname(p["name"]) or p["name"],
                }
    return list(seen.values())


def pick_priority_owner(owners: list[dict], jsm_requester: str = "") -> dict:
    """
    한 대상의 여러 담당자 후보 중 1순위를 정한다.
    이 시스템에 연결된 JIRA 티켓 중 가장 최근 것의 JSM요청자와 이름이 일치하는 담당자가
    있으면 그 사람을 1순위로 (실제로 최근에 그 사람이 변경작업을 요청했다는 뜻이므로),
    없으면 엑셀 담당자 칸에 적힌 순서 그대로 첫 번째 사람을 1순위로 쓴다.
    """
    if jsm_requester:
        target = clean_name(jsm_requester)
        for o in owners:
            if clean_name(o["name"]) == target:
                return o
    return owners[0]


def group_and_vote(items: list[dict], key_fn, include_fn) -> list[dict]:
    """
    공통 그룹핑 로직 (DR훈련/용량관리 리마인드가 공유).
    items를 key_fn(d) 기준으로 묶고, 그룹 안에서 각 대상의 1순위 담당자에게 표를 준다.
    include_fn(d)이 False인 대상은 건너뛴다.
    반환: [{"key": ..., "items": [...], "votes": {raw_owner: count}}, ...]
    """
    groups: dict[str, dict] = {}
    for d in items:
        if not include_fn(d):
            continue
        owners = parse_owners(d.get("owner", ""))
        if not owners:
            continue

        key = key_fn(d) or "미지정"
        g = groups.setdefault(key, {"key": key, "items": [], "votes": {}})
        g["items"].append(d)
        top = pick_priority_owner(owners, d.get("jsm_requester", ""))
        g["votes"][top["raw"]] = g["votes"].get(top["raw"], 0) + 1
    return list(groups.values())


def pick_representative(g: dict) -> tuple[str, dict, str]:
    """그룹 내 1순위 최다 득표 담당자 선정. 반환: (top_raw, parsed_owner, given)"""
    top_raw = max(g["votes"], key=g["votes"].get)  # 그룹 내 최다 1순위 = 대표 담당자
    p = parse_owners(top_raw)[0]
    given = strip_surname(p["name"]) or p["name"] or "담당자"
    return top_raw, p, given


def is_upcoming(item: dict, as_of: date, days: int) -> bool:
    """작업 예정일이 오늘~N일 뒤 사이인 대상 (완료된 건 제외)"""
    sched = item.get("schedule")
    if not sched or item.get("completed"):
        return False
    return as_of <= sched <= as_of + timedelta(days=days)


def group_unplanned_by_service(
    details: list[dict],
    cmdb_map: dict | None = None,
    hinted: bool | None = None,
    kind: str = "blank",
    as_of: date | None = None,
) -> list[dict]:
    """
    미계획(일정 미등록) 대상을 '서비스'(주업무명) 단위로 묶고 초안까지 생성.

    같은 서비스 소속 시스템들도 엑셀 행마다 담당자 나열 순서가 제각각이라(예: 동일
    서비스의 시스템들이 담당자 A/B/C를 행마다 다른 순서로 등록), 예전처럼
    '행별 1순위 담당자'로만 나누면 같은 서비스가 여러 명에게 조각조각 흩어져 보내진다.
    그래서 서비스로 먼저 묶고, 그 서비스 안에서 1순위로 가장 많이 등장한 사람을
    대표 담당자로 뽑아 그 한 명에게만 보낸다 (받는 담당자는 미리보기에서 수동 변경 가능).

    hinted 필터 (같은 '미계획' 안에서도 성격이 다른 두 경우를 분리):
    - None: 전체 미계획
    - False: 일정칸이 완전히 빈 대상만 ("아예 미기입")
    - True : '11월 예정'처럼 텍스트는 있지만 날짜로 파싱 안 된 대상만 ("대략적 일정만 기입")

    cmdb_map(item_no -> CMDB 자산)이 주어지면, DM 발송 대상 팀명을 CMDB 기준으로 보정한다.
    반환: [{owner, service, name, team, given, count, targets, message}, ...] (대상 많은 순)
    """
    if kind == "upcoming":
        # 사전 안내는 '미계획'이 아니라 '일정이 코앞인 대상'이 모수다
        today = as_of or date.today()
        include_fn = lambda d: is_upcoming(d, today, settings.pre_work_remind_days)  # noqa: E731
    else:
        include_fn = lambda d: (  # noqa: E731
            not d.get("planned") and (hinted is None or has_schedule_hint(d) == hinted)
        )

    raw_groups = group_and_vote(
        details,
        key_fn=lambda d: d.get("business_name") or d.get("system_name"),
        include_fn=include_fn,
    )

    msg_kind = kind if kind == "upcoming" else ("hinted" if hinted else "blank")
    result = []
    for g in raw_groups:
        top_raw, p, given = pick_representative(g)
        team = team_via_cmdb(p["name"], g["items"], cmdb_map, p["team"])
        result.append({
            "owner": top_raw,          # 이름-팀 (선택 키, 엑셀 원본 기준)
            "service": g["key"],
            "name": p["name"],
            "team": team,               # DM 발송용 (CMDB 보정)
            "given": given,
            "count": len(g["items"]),
            "targets": g["items"],     # 'items'는 Jinja에서 dict.items()와 충돌 → targets
            "greeting_suffix": greeting_suffix(g["items"], msg_kind),  # 이름 뒤 고정 문구+대상
            "candidates": candidates_of(g["items"], cmdb_map),  # 받는 담당자 후보
            "message": build_message(given, g["items"], msg_kind),
        })

    return sorted(result, key=lambda x: (-x["count"], x["service"]))
