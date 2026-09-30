# tests/test_team_lookup.py
"""팀별 대상 조회에서 건수와 링크를 만드는 로직 (app/web/routes/teams.py)."""
from app.web.routes.teams import build_rows, match_teams, split_team_names, team_options


# 엑셀 운영팀 칸은 한 팀만 적힌 행과 여러 팀이 묶인 행이 섞여 있다
TEAMS = ["라이브쇼핑팀", "라이브쇼핑팀||재무서비스팀", "커머스사업2그룹", "재무서비스팀"]


class TestTeamSearch:
    """'라이브쇼핑팀'으로 찾으면 그 이름이 들어간 행이 다 나와야 한다."""

    def test_묶인_이름도_같이_찾는다(self):
        assert match_teams(TEAMS, ["라이브쇼핑팀"]) == [
            "라이브쇼핑팀", "라이브쇼핑팀||재무서비스팀"
        ]

    def test_뒤에_묶인_팀으로도_찾는다(self):
        assert match_teams(TEAMS, ["재무서비스팀"]) == [
            "라이브쇼핑팀||재무서비스팀", "재무서비스팀"
        ]

    def test_묶인_이름_전체로_찾으면_그것만(self):
        assert match_teams(TEAMS, ["라이브쇼핑팀||재무서비스팀"]) == ["라이브쇼핑팀||재무서비스팀"]

    def test_여러_검색어는_합집합(self):
        assert match_teams(TEAMS, ["커머스", "재무서비스팀"]) == [
            "라이브쇼핑팀||재무서비스팀", "커머스사업2그룹", "재무서비스팀"
        ]

    def test_검색어가_없으면_전체(self):
        assert match_teams(TEAMS, []) == TEAMS

    def test_대소문자_구분_안_한다(self):
        assert match_teams(["Live Shopping팀"], ["live shopping"]) == ["Live Shopping팀"]

    def test_없는_이름은_빈_목록(self):
        assert match_teams(TEAMS, ["없는팀"]) == []


class TestTeamOptions:
    """검색 후보에는 묶인 값을 쪼갠 낱개 팀 이름도 들어간다."""

    def test_묶인_값을_쪼개_후보에_넣는다(self):
        assert team_options(["라이브쇼핑팀||재무서비스팀"]) == [
            "라이브쇼핑팀", "라이브쇼핑팀||재무서비스팀", "재무서비스팀"
        ]

    def test_중복은_한_번만(self):
        assert team_options(["라이브쇼핑팀", "라이브쇼핑팀"]) == ["라이브쇼핑팀"]

    def test_구분자_표기가_섞여_있어도_쪼갠다(self):
        # 담당자 칸과 같은 표기라 파이프 하나/쉼표도 구분자로 본다
        assert split_team_names("A팀|B팀, C팀") == ["A팀", "B팀", "C팀"]


def _domains():
    """DR(화면 하나) + 용량관리(DATA/ARCH로 갈림) 두 영역짜리 최소 구성"""
    return [
        {
            "code": "DR",
            "label": "DR훈련",
            "parts": [{"label": "", "href": "/dr?half=H2&team="}],
            "by_team": {"데이터센터팀": {"total": 5, "done": 2}},
        },
        {
            "code": "CAP",
            "label": "용량관리",
            "parts": [
                {"label": "DATA", "href": "/capacity?sheet=DATA&team="},
                {"label": "ARCH", "href": "/capacity?sheet=ARCH&team="},
            ],
            "by_team": {
                "데이터센터팀": {"total": 12, "done": 7, "split": {"DATA": 8, "ARCH": 4}},
                "커머스팀": {"total": 3, "done": 0, "split": {"DATA": 3}},
            },
        },
    ]


def _cell(row, code):
    return next(c for c in row["cells"] if c["code"] == code)


class TestBuildRows:
    def test_화면이_하나인_영역은_건수_자체가_링크가_된다(self):
        row = build_rows(_domains(), ["데이터센터팀"])[0]
        dr = _cell(row, "DR")
        assert dr["total"] == 5 and dr["done"] == 2
        assert dr["links"] == [
            {"label": "", "count": 5, "href": "/dr?half=H2&team=%EB%8D%B0%EC%9D%B4%ED%84%B0%EC%84%BC%ED%84%B0%ED%8C%80"}
        ]

    def test_화면이_갈리는_영역은_합계와_화면별_링크를_같이_준다(self):
        cap = _cell(build_rows(_domains(), ["데이터센터팀"])[0], "CAP")
        assert cap["total"] == 12
        assert [(x["label"], x["count"]) for x in cap["links"]] == [("DATA", 8), ("ARCH", 4)]

    def test_건수가_0인_화면은_링크에서_뺀다(self):
        # ARCH에 없는 팀인데 ARCH 링크를 주면 눌렀을 때 빈 목록이 나온다
        cap = _cell(build_rows(_domains(), ["커머스팀"])[0], "CAP")
        assert cap["total"] == 3
        assert [(x["label"], x["count"]) for x in cap["links"]] == [("DATA", 3)]

    def test_그_영역에_대상이_없는_팀은_0건으로_둔다(self):
        row = build_rows(_domains(), ["커머스팀"])[0]
        dr = _cell(row, "DR")
        assert dr["total"] == 0 and dr["links"] == []

    def test_대상_수는_영역_합계다(self):
        rows = {r["team"]: r for r in build_rows(_domains(), ["데이터센터팀", "커머스팀"])}
        assert rows["데이터센터팀"]["total"] == 5 + 12
        assert rows["커머스팀"]["total"] == 3

    def test_팀_이름은_링크에서_이스케이프된다(self):
        domains = [{
            "code": "DR", "label": "DR훈련",
            "parts": [{"label": "", "href": "/dr?team="}],
            "by_team": {"A&B 팀": {"total": 1, "done": 0}},
        }]
        href = build_rows(domains, ["A&B 팀"])[0]["cells"][0]["links"][0]["href"]
        assert href == "/dr?team=A%26B%20%ED%8C%80"
