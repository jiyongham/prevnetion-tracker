import os

_DUMMY_ENV = {
    "JIRA_URL": "https://jira.dummy.test",
    "JIRA_PAT": "dummy-jira-pat",
    "JIRA_PROJECT": "DUMMY",
    "SENDER_TEAM": "테스트팀",
    "SENDER_NAME": "테스트담당자",
    "CAPACITY_SENDER_NAME": "테스트용량담당자",
    "DASHBOARD_URL": "http://dummy-dashboard.test:8000",
    "ADMIN_USERS": "테스트관리자",
    "CAPACITY_ADMIN_USERS": "테스트용량관리자",
    "EOS_ADMIN_USERS": "테스트EoS관리자",
}
for _k, _v in _DUMMY_ENV.items():
    os.environ[_k] = _v  # force-override .env file values

import time
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

routes = [
    ("GET", "/"),
    ("GET", "/dr"),
    ("GET", "/logs"),
    ("GET", "/remind-preview"),
    ("GET", "/owner-check"),
    ("GET", "/capacity"),
    ("GET", "/capacity/remind-preview"),
    ("GET", "/capacity/owner-check"),
    ("GET", "/eos"),
    ("GET", "/eos/remind-preview"),
    ("GET", "/eos/owner-check"),
    ("GET", "/eos/plan-chat"),
    ("GET", "/kernel"),
    ("GET", "/kernel/owner-check"),
]

for method, path in routes:
    t = time.time()
    try:
        resp = client.request(method, path)
        elapsed = time.time() - t
        body_snip = resp.text[:150].replace("\n", " ")
        print(f"{path:35s} status={resp.status_code} elapsed={elapsed:.2f}s body={body_snip}")
    except Exception as e:
        elapsed = time.time() - t
        print(f"{path:35s} EXC {type(e).__name__}: {str(e)[:150]} elapsed={elapsed:.2f}s")
