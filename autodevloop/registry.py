"""A small registry of known project directories, stored in the user home.

Lets the web dashboard list every project that was started via the CLI or the
web UI, regardless of where it lives on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import load_json, now_text, save_json


def registry_path() -> Path:
    return Path.home() / ".autodevloop" / "registry.json"


def load() -> list[dict[str, Any]]:
    data = load_json(registry_path(), {"projects": []})
    projects = data.get("projects", []) if isinstance(data, dict) else []
    return [p for p in projects if isinstance(p, dict)]


def register(root: Path, name: str = "") -> None:
    root = root.resolve()
    projects = load()
    entry = {"dir": str(root), "name": name or root.name, "registered_at": now_text()}
    projects = [p for p in projects if p.get("dir") != str(root)]
    projects.insert(0, entry)
    save_json(registry_path(), {"projects": projects[:100]}, stamp=False)


def remove(root: Path) -> None:
    root = root.resolve()
    projects = [p for p in load() if p.get("dir") != str(root)]
    save_json(registry_path(), {"projects": projects}, stamp=False)
