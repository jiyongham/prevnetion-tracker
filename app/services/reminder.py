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
        f"다름아니라 공지드린 하반기 DR 훈련 하기 미계획 대상에 대해 "
        f"계획된 일정 알 수 있을까요?\n"
        f"{build_body(items)}"
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


def group_unplanned_by_owner(details: list[dict], cmdb_map: dict | None = None) -> list[dict]:
    """
    미계획(일정 미등록) 대상을 담당자별로 그룹핑하고 초안까지 생성.
    한 대상에 담당자가 여러 명이면 '대표 담당자(목록의 첫 번째)'에게만 배정한다.
    → 같은 시스템이 여러 담당자에게 중복 노출되지 않음.
    cmdb_map(item_no -> CMDB 자산)이 주어지면, DM 발송 대상 팀명을 CMDB 기준으로 보정한다.
    반환: [{owner, name, team, given, count, items, message}, ...] (대상 많은 순)
    """
    groups: dict[str, dict] = {}
    for d in details:
        if d.get("planned"):
            continue
        owners = parse_owners(d.get("owner", ""))
        if not owners:
            continue
        p = owners[0]  # 대표 담당자 (첫 번째)
        key = p["raw"] or "미지정"
        g = groups.setdefault(key, {
            "owner": key, "name": p["name"], "team": p["team"], "items": [],
        })
        g["items"].append(d)

    result = []
    for g in groups.values():
        given = strip_surname(g["name"]) or g["name"] or "담당자"
        team = team_via_cmdb(g["name"], g["items"], cmdb_map, g["team"])
        result.append({
            "owner": g["owner"],      # 이름-팀 (선택 키, 엑셀 원본 기준)
            "name": g["name"],
            "team": team,              # DM 발송용 (CMDB 보정)
            "given": given,
            "count": len(g["items"]),
            "targets": g["items"],    # 'items'는 Jinja에서 dict.items()와 충돌 → targets
            "greeting_suffix": greeting_suffix(g["items"]),  # 이름 뒤 고정 문구+대상
            "candidates": candidates_of(g["items"], cmdb_map),  # 받는 담당자 후보
            "message": build_message(given, g["items"]),
        })

    return sorted(result, key=lambda x: (-x["count"], x["owner"]))
