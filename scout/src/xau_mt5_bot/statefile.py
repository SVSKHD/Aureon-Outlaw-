"""Crash-safe JSON state files (v2.0.0 item 8).

Writes go to a temporary file in the same directory, are flushed and fsynced, then atomically replace the target
(`os.replace` is atomic on both NTFS and POSIX). A previous good copy is kept as `<name>.bak`; if the main file is
unreadable or malformed the backup is loaded instead, and the caller is told which source was used so it can audit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, default=str)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if target.exists():
        try:
            os.replace(target, target.with_name(target.name + ".bak"))
        except OSError:
            pass
    os.replace(tmp, target)


def load_json_state(path: str | Path) -> tuple[dict[str, Any], str]:
    """Return (state, source) where source is 'main', 'backup' or 'none'. Never raises."""
    target = Path(path)
    for candidate, label in ((target, "main"), (target.with_name(target.name + ".bak"), "backup")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data, label
        except Exception:
            continue
    return {}, "none"
