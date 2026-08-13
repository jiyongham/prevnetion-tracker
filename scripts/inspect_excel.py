# scripts/inspect_excel.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.excel_loader import inspect_columns

if __name__ == "__main__":
    inspect_columns()
