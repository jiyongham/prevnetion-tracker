# scripts/diag_confluence_siblings.py
"""
EoS 차주 계획서 페이지 구조 파악용. 주어진 pageId의 형제 페이지 목록과,
각 형제 페이지 안에 있는 표(계획서 이름들)를 확인한다.

사용법:
  python -m scripts.diag_confluence_siblings 545266110
"""
import re
import sys

from app.core.confluence_client import confluence

_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html: str) -> str:
    return _TAG_RE.sub(" ", html or "").strip()


def main():
    if len(sys.argv) < 2:
        print("사용법: python -m scripts.diag_confluence_siblings <pageId>")
        return
    page_id = sys.argv[1]

    page = confluence.get_content(page_id, expand="ancestors")
    print(f"기준 페이지: {page['id']} {page['title']!r}")

    ancestors = page.get("ancestors", [])
    if not ancestors:
        print("⚠ 부모 페이지 없음 (최상위 페이지) - 형제 조회 불가")
        return
    parent = ancestors[-1]
    print(f"부모 페이지: {parent['id']} {parent['title']!r}\n")

    siblings = confluence.get_children(parent["id"])
    print(f"형제 페이지 {len(siblings)}개:")
    for s in siblings:
        marker = " <- 기준" if s["id"] == page_id else ""
        print(f"  {s['id']}  {s['title']!r}{marker}")

    print("\n--- 기준 페이지 본문에서 표 미리보기 (앞부분) ---")
    body = confluence.get_content(page_id, expand="body.storage")
    storage = body.get("body", {}).get("storage", {}).get("value", "")

    tables = re.findall(r"<table.*?</table>", storage, re.S)
    print(f"표 {len(tables)}개 발견")
    for i, t in enumerate(tables[:3]):
        print(f"\n[표 {i+1}] (텍스트만 추출, 앞 800자)")
        print(strip_tags(t)[:800])


if __name__ == "__main__":
    main()
