# scripts/run_remind.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.report import send_remind

if __name__ == "__main__":
    half = sys.argv[1] if len(sys.argv) > 1 else None
    send_remind(half=half)
