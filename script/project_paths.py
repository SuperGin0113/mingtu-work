from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT_DIR / "script"
DATA_DIR = ROOT_DIR / "data"
TMP_DIR = ROOT_DIR / "tmp"
ENV_FILE = ROOT_DIR / ".env"
LIST_ITEMS_FILE = ROOT_DIR / "list_items.json"

TMP_DIR.mkdir(parents=True, exist_ok=True)
