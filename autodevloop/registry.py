"""A small registry of known project directories, stored in the user home.

Lets the web dashboard list every project that was started via the CLI or the
web UI, regardless of where it lives on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import load_json, normalize_project_path, now_text, save_json


def registry_path() -> Path:
    return Path.home() / ".autodevloop" / "registry.json"


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def load() -> list[dict[str, Any]]:
    data = load_json(registry_path(), {"projects": []})
    projects = data.get("projects", []) if isinstance(data, dict) else []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for project in projects:
        if not isinstance(project, dict) or not str(project.get("dir") or "").strip():
            continue
        entry = dict(project)
        original_dir = str(entry["dir"])
        root = normalize_project_path(str(entry["dir"]))
        entry["dir"] = str(root)
        if original_dir != entry["dir"]:
            entry["_raw_dir"] = original_dir
        if entry["dir"] in seen:
            continue
        seen.add(entry["dir"])
        normalized.append(entry)
    return normalized


def register(root: Path, name: str = "") -> None:
    root = normalize_project_path(str(root))
    projects = load()
    entry = {"dir": str(root), "name": name or root.name, "registered_at": now_text()}
    projects = [p for p in projects if p.get("dir") != str(root)]
    projects.insert(0, entry)
    save_json(registry_path(), {"projects": [_public(p) for p in projects[:100]]}, stamp=False)


def remove(root: Path) -> None:
    root = normalize_project_path(str(root))
    projects = [p for p in load() if p.get("dir") != str(root)]
    save_json(registry_path(), {"projects": [_public(p) for p in projects]}, stamp=False)
