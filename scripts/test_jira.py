# scripts/test_jira.py
"""JIRA 연결 및 필드 구조 확인용 테스트 스크립트"""
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.jira_client import jira
from app.config import settings


def test_connection():
    print("=" * 50)
    print(f"JIRA URL: {settings.jira_url}")
    print(f"Project : {settings.jira_project}")
    print("=" * 50)

    # 1. [예방] 티켓 검색
    jql = f'project = {settings.jira_project} AND summary ~ "예방2" ORDER BY created DESC'
    print(f"\n[JQL] {jql}\n")

    issues = jira.search(jql, max_results=5)
    print(f"조회된 티켓 수: {len(issues)}건\n")

    if not issues:
        print("⚠️ 티켓이 없습니다. JQL/권한 확인 필요")
        return

    # 2. 첫 티켓의 전체 필드 출력 (커스텀 필드 id 파악용)
    first_key = issues[0]["key"]
    print(f"[{first_key}] 전체 필드 확인 (변경 계획 완료일 찾기)")
    print("-" * 50)

    detail = jira.get_issue(first_key)
    for field_id, value in detail["fields"].items():
        if value is not None:  # 값 있는 필드만
            print(f"{field_id}: {str(value)[:80]}")

    # 3. 티켓 요약
    print("\n" + "=" * 50)
    print("티켓 목록 요약")
    print("=" * 50)
    for issue in issues:
        f = issue["fields"]
        print(f"{issue['key']} | {f['status']['name']} | {f['summary'][:40]}")


if __name__ == "__main__":
    test_connection()
