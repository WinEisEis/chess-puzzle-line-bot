"""Remembers which LINE groups the bot is in (simple JSON file)."""
from __future__ import annotations

import json
import threading

from .config import settings

_lock = threading.Lock()


def _path():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / "groups.json"


def _load() -> list[str]:
    p = _path()
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text("utf-8")))
    except (json.JSONDecodeError, OSError):
        return []


def add_group(group_id: str) -> None:
    with _lock:
        groups = _load()
        if group_id not in groups:
            groups.append(group_id)
            _path().write_text(json.dumps(groups, indent=2), "utf-8")


def remove_group(group_id: str) -> None:
    with _lock:
        groups = [g for g in _load() if g != group_id]
        _path().write_text(json.dumps(groups, indent=2), "utf-8")


def all_groups() -> list[str]:
    with _lock:
        merged = _load()
    for g in settings.extra_group_ids:
        if g not in merged:
            merged.append(g)
    return merged
