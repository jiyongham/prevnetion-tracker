# app/services/reminder.py
import re

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
    이름에서 성 제외 (홍길동 -> 지용).
    두 글자 성(남궁/선우 등)은 두 글자, 그 외에는 한 글자 제거.
    """
    name = clean_name(name)
    if len(name) <= 1:
        return name
    if name[:2] in TWO_CHAR_SURNAMES:
        return name[2:]
    return name[1:]


def build_body(items: list[dict]) -> str:
    """대상 목록 본문 (시스템명 / 호스트명 / IP)"""
    lines = [
        f"{d.get('system_name', '')} / {d.get('hostname', '')} / {d.get('ip', '')}"
        for d in items
    ]
    return "\n".join(lines) if lines else "(대상 없음)"


def greeting_suffix(items: list[dict]) -> str:
    """인사말에서 '이름' 뒤에 붙는 고정 문구 + 대상 목록 (이름만 바꿔치기 가능하도록 분리)"""
    return (
        f"님. {settings.sender_team} {settings.sender_name}입니다.\n\n"
        f"다름아니라 공지드린 하반기 DR 훈련 하기 계획된 일정 알 수 있을까요?\n\n"
        f"{build_body(items)}\n\n"
        f"DR 모의훈련 진척 현황({settings.dashboard_url})에 기입 요청드립니다."
    )


def build_message(given: str, items: list[dict]) -> str:
    """담당자 1인에게 보낼 미계획 리마인드 초안 (전체 문자열)"""
    return f"안녕하세요, {given}{greeting_suffix(items)}"


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


def group_unplanned_by_service(details: list[dict], cmdb_map: dict | None = None) -> list[dict]:
    """
    미계획(일정 미등록) 대상을 '서비스'(주업무명) 단위로 묶고 초안까지 생성.

    같은 서비스 소속 시스템들도 엑셀 행마다 담당자 나열 순서가 제각각이라(예: 블라섬
    서비스의 시스템들이 홍길동/홍길동/홍길동을 행마다 다른 순서로 등록), 예전처럼
    '행별 1순위 담당자'로만 나누면 같은 서비스가 여러 명에게 조각조각 흩어져 보내진다.
    그래서 서비스로 먼저 묶고, 그 서비스 안에서 1순위로 가장 많이 등장한 사람을
    대표 담당자로 뽑아 그 한 명에게만 보낸다 (받는 담당자는 미리보기에서 수동 변경 가능).

    cmdb_map(item_no -> CMDB 자산)이 주어지면, DM 발송 대상 팀명을 CMDB 기준으로 보정한다.
    반환: [{owner, service, name, team, given, count, targets, message}, ...] (대상 많은 순)
    """
    groups: dict[str, dict] = {}
    for d in details:
        if d.get("planned"):
            continue
        owners = parse_owners(d.get("owner", ""))
        if not owners:
            continue

        service = d.get("business_name") or d.get("system_name") or "미지정"
        g = groups.setdefault(service, {"service": service, "items": [], "votes": {}})
        g["items"].append(d)
        raw = owners[0]["raw"]  # 이 대상의 1순위 담당자에게 표 하나
        g["votes"][raw] = g["votes"].get(raw, 0) + 1

    result = []
    for g in groups.values():
        top_raw = max(g["votes"], key=g["votes"].get)  # 서비스 내 최다 1순위 = 대표 담당자
        p = parse_owners(top_raw)[0]
        given = strip_surname(p["name"]) or p["name"] or "담당자"
        team = team_via_cmdb(p["name"], g["items"], cmdb_map, p["team"])
        result.append({
            "owner": top_raw,          # 이름-팀 (선택 키, 엑셀 원본 기준)
            "service": g["service"],
            "name": p["name"],
            "team": team,               # DM 발송용 (CMDB 보정)
            "given": given,
            "count": len(g["items"]),
            "targets": g["items"],     # 'items'는 Jinja에서 dict.items()와 충돌 → targets
            "greeting_suffix": greeting_suffix(g["items"]),  # 이름 뒤 고정 문구+대상
            "candidates": candidates_of(g["items"], cmdb_map),  # 받는 담당자 후보
            "message": build_message(given, g["items"]),
        })

    return sorted(result, key=lambda x: (-x["count"], x["service"]))
