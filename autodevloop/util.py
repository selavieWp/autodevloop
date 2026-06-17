"""Filesystem, JSON, and text helpers shared across AutoDevLoop modules."""

from __future__ import annotations

import datetime as dt
import filecmp
import json
import os
import re
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

APP_DIR = ".autodev"
CONFIG_FILE = ".autodevloop.yml"
STATE_FILE = "state.json"
PROGRESS_FILE = "progress.json"
STOP_FILE = "STOP"

TEXT_SUFFIXES = {
    ".css", ".html", ".js", ".json", ".jsx", ".md", ".mjs", ".py",
    ".toml", ".ts", ".tsx", ".txt", ".yml", ".yaml", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cpp", ".h",
    ".sh", ".sql", ".env", ".cfg", ".ini",
}
DOC_SUFFIXES = {".md", ".txt", ".rst"}
INTERNAL_DIRS = {APP_DIR, ".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "dist", "build"}


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def slugify(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")
    return cleaned or fallback


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return default


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def save_json(path: Path, data: Any, stamp: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if stamp and isinstance(data, dict):
        data["updated_at"] = now_text()
    # Unique temp name so concurrent writers never collide on the same file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(json_safe(data), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def _chmod_retry(func, path, _exc) -> None:
    """Clear the read-only bit (common on Windows .git objects) and retry."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree_robust(path: Path) -> None:
    if not path.exists():
        return
    try:  # Python 3.12+ uses onexc; older uses onerror.
        shutil.rmtree(path, onexc=_chmod_retry)
    except TypeError:
        shutil.rmtree(path, onerror=_chmod_retry)


def safe_rmtree(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise RuntimeError(f"refusing to delete outside project: {resolved}") from exc
    rmtree_robust(path)


def restore_working_dir(before_dir: Path, current_dir: Path) -> None:
    """Restore current_dir's working files from a snapshot, keeping .git intact."""
    if current_dir.exists():
        for item in current_dir.iterdir():
            if item.name in INTERNAL_DIRS:
                continue
            if item.is_dir():
                rmtree_robust(item)
            else:
                try:
                    item.unlink()
                except OSError:
                    pass
    copy_tree_contents(before_dir, current_dir)


def copy_tree_contents(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for item in source.iterdir():
        if item.name in INTERNAL_DIRS:
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def list_generated_files(base_dir: Path) -> list[str]:
    if not base_dir.exists():
        return []
    files: list[str] = []
    for path in sorted(base_dir.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(base_dir)
        if any(part in INTERNAL_DIRS for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return files


def collect_context(base_dir: Path, max_bytes: int = 70_000) -> str:
    """Render a file tree plus readable file bodies, recency-prioritised."""
    if not base_dir.exists():
        return "(empty)"

    tree: list[str] = ["File tree:"]
    files: list[Path] = []
    for path in sorted(base_dir.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(base_dir)
        if any(part in INTERNAL_DIRS for part in relative.parts):
            continue
        files.append(path)
        tree.append(f"- {relative.as_posix()}")

    parts = list(tree)
    parts.append("\nReadable files (most recently modified first):")
    used = len("\n".join(parts).encode("utf-8"))
    readable = [p for p in files if p.suffix.lower() in TEXT_SUFFIXES]
    readable.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for path in readable:
        relative = path.relative_to(base_dir).as_posix()
        block = f'\n<existing_file path="{relative}">\n{read_text(path)}\n</existing_file>\n'
        size = len(block.encode("utf-8"))
        if used + size > max_bytes:
            parts.append("\n(context truncated; older files omitted)")
            break
        parts.append(block)
        used += size
    return "\n".join(parts)


def diff_file_lists(before_dir: Path, after_dir: Path) -> dict[str, list[str]]:
    before = set(list_generated_files(before_dir))
    after = set(list_generated_files(after_dir))
    changed: list[str] = []
    for relative in sorted(before & after):
        left = before_dir / relative
        right = after_dir / relative
        try:
            same = filecmp.cmp(left, right, shallow=False)
        except OSError:
            same = False
        if not same:
            changed.append(relative)
    return {
        "added": sorted(after - before),
        "changed": changed,
        "removed": sorted(before - after),
    }


def markdown_list(items: list[Any]) -> str:
    values = [str(item).strip() for item in (items or []) if str(item).strip()]
    return "\n".join(f"- {item}" for item in values) if values else "- None"


def extract_json(raw: str, default: dict[str, Any]) -> dict[str, Any]:
    """Best-effort JSON extraction from a model response (handles code fences)."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            value = json.loads(fence.group(1))
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    objects: list[tuple[int, dict[str, Any]]] = []
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append((len(json.dumps(value)), value))
    return max(objects, key=lambda item: item[0])[1] if objects else default
