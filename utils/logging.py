from __future__ import annotations

import sys
import time

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
