# scripts/run_report.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.report import send_report

if __name__ == "__main__":
    half = sys.argv[1] if len(sys.argv) > 1 else None
    # JIRA 연동 없이 엑셀만 테스트하려면: use_jira=False
    send_report(half=half, use_jira=True)
