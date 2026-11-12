"""Tiny GUI status bridge. Safe to import from bot.py; no GUI dependency."""
import json, os, tempfile
from datetime import datetime

DATA_DIR = "data"
STATUS_FILE = os.path.join(DATA_DIR, "runtime_status.json")

def update_gui_status(stage: str, detail: str = "", backend: str = ""):
    tmp = None
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {
            "stage": stage,
            "detail": detail,
            "backend": backend,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
        }
        fd, tmp = tempfile.mkstemp(prefix="runtime_", suffix=".json", dir=DATA_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_FILE)
        tmp = None
    except Exception:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
