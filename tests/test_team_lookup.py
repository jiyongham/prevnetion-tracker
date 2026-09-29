# tests/test_team_lookup.py
"""팀별 대상 조회에서 건수와 링크를 만드는 로직 (app/web/routes/teams.py)."""
from app.web.routes.teams import build_rows


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
