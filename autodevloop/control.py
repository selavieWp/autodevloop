"""Durable run checkpoints, human directives, and process metadata."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .util import copy_tree_contents, load_json, now_text, restore_working_dir, save_json, safe_rmtree, write_text

CHECKPOINT_FILE = "checkpoint.json"
DIRECTIVES_FILE = "directives.json"
RUN_CONTROL_FILE = "run-control.json"


def checkpoint_path(app_dir: Path) -> Path:
    return app_dir / CHECKPOINT_FILE


def load_checkpoint(app_dir: Path) -> dict[str, Any]:
    data = load_json(checkpoint_path(app_dir), {})
    return data if isinstance(data, dict) else {}


def save_checkpoint(app_dir: Path, data: dict[str, Any]) -> None:
    save_json(checkpoint_path(app_dir), data)


def clear_checkpoint(app_dir: Path) -> None:
    path = checkpoint_path(app_dir)
    if path.exists():
        path.unlink()
    base = app_dir / "checkpoints"
    for active in [base / "active", base / "active.previous", *base.glob("active.staging.*")]:
        if active.exists():
            safe_rmtree(active, app_dir.parent)


def snapshot_active(app_dir: Path, source: Path) -> Path:
    base = app_dir / "checkpoints"
    active = base / "active"
    previous = base / "active.previous"
    staging = base / f"active.staging.{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        safe_rmtree(staging, app_dir.parent)
    staging.mkdir(parents=True, exist_ok=True)
    copy_tree_contents(source, staging)
    if previous.exists():
        safe_rmtree(previous, app_dir.parent)
    if active.exists():
        active.replace(previous)
    staging.replace(active)
    if previous.exists():
        safe_rmtree(previous, app_dir.parent)
    return active


def restore_active(app_dir: Path, target: Path) -> bool:
    active = app_dir / "checkpoints" / "active"
    previous = app_dir / "checkpoints" / "active.previous"
    if not active.exists() and previous.exists():
        previous.replace(active)
    if not active.exists():
        return False
    restore_working_dir(active, target)
    return True


def load_directives(app_dir: Path) -> list[dict[str, Any]]:
    data = load_json(app_dir / DIRECTIVES_FILE, {"directives": []})
    items = data.get("directives", []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


def save_directives(app_dir: Path, items: list[dict[str, Any]]) -> None:
    save_json(app_dir / DIRECTIVES_FILE, {"directives": items}, stamp=False)


def add_directive(app_dir: Path, text: str, scope: str, version: int) -> dict[str, Any]:
    if scope not in {"next", "version", "future"}:
        scope = "version"
    item = {
        "id": uuid.uuid4().hex[:12], "text": text.strip(), "scope": scope,
        "version": int(version), "active": True, "created_at": now_text(),
        "applied_to": [],
    }
    items = load_directives(app_dir)
    items.append(item)
    save_directives(app_dir, items)
    return item


def set_directive_active(app_dir: Path, directive_id: str, active: bool) -> bool:
    items = load_directives(app_dir)
    found = False
    for item in items:
        if item.get("id") == directive_id:
            item["active"] = bool(active)
            found = True
    if found:
        save_directives(app_dir, items)
    return found


def applicable_directives(app_dir: Path, version: int) -> list[dict[str, Any]]:
    result = []
    for item in load_directives(app_dir):
        if not item.get("active") or not str(item.get("text", "")).strip():
            continue
        scope = item.get("scope")
        origin = int(item.get("version", 0) or 0)
        if scope == "future" or (scope in {"next", "version"} and origin == version):
            result.append(item)
    return result


def render_directives(app_dir: Path, version: int) -> str:
    items = applicable_directives(app_dir, version)
    if not items:
        return ""
    lines = [
        "\n\nHUMAN OVERRIDE DIRECTIVES (highest priority):",
        "If these conflict with AI-generated plans, reviews, or earlier agent output, follow the latest human directive.",
    ]
    lines.extend(f"- [{item.get('scope')}] {item.get('text')}" for item in items)
    return "\n".join(lines)


def mark_directives_applied(app_dir: Path, version: int, step_id: str) -> None:
    items = load_directives(app_dir)
    changed = False
    for item in items:
        if item not in applicable_directives(app_dir, version):
            continue
        applied = item.setdefault("applied_to", [])
        if step_id not in applied:
            applied.append(step_id)
            changed = True
        if item.get("scope") == "next":
            item["active"] = False
            changed = True
    if changed:
        save_directives(app_dir, items)


def write_progress_doc(root: Path, checkpoint: dict[str, Any], state: dict[str, Any]) -> None:
    completed = checkpoint.get("completed_steps", []) or []
    lines = [
        "# Development progress", "",
        "> Generated by AutoDevLoop. This document is read-only in the dashboard.", "",
        f"- Status: **{checkpoint.get('status') or state.get('status', 'initialized')}**",
        f"- Version: **v{checkpoint.get('version', state.get('current_version', 0))}**",
        f"- Phase: **{checkpoint.get('phase', state.get('phase', 'build'))}**",
        f"- Last completed agent: **{checkpoint.get('last_completed_agent') or 'none'}**",
        f"- Next agent: **{checkpoint.get('next_agent') or 'none'}**",
        f"- Updated: {checkpoint.get('updated_at') or now_text()}", "",
        "## Completed steps", "",
    ]
    lines.extend(f"- {step}" for step in completed)
    if not completed:
        lines.append("- None")
    if checkpoint.get("pause_reason"):
        lines.extend(["", "## Pause reason", "", str(checkpoint["pause_reason"])])
    lines.extend(["", "## Resume", "", "Use the dashboard Continue button or `autodevloop resume --project-dir <path>`.\n"])
    write_text(root / "docs" / "development-progress.md", "\n".join(lines))


def write_run_control(app_dir: Path, pid: int, project: Path) -> None:
    pgid = pid
    if os.name != "nt":
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pass
    save_json(app_dir / RUN_CONTROL_FILE, {
        "pid": pid, "pgid": pgid, "project": str(project.resolve()),
        "started_at": now_text(), "started_ts": time.time(),
    }, stamp=False)


def persisted_process_alive(app_dir: Path, project: Path) -> bool:
    meta = load_json(app_dir / RUN_CONTROL_FILE, {})
    pid = int(meta.get("pid", 0) or 0)
    if not pid or str(meta.get("project", "")) != str(project.resolve()):
        return False
    try:
        if os.name != "nt":
            cmdline_path = Path("/proc") / str(pid) / "cmdline"
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().decode("utf-8", "replace").replace("\0", " ")
                if "autodevloop" not in cmdline or str(project.resolve()) not in cmdline:
                    return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def clear_run_control(app_dir: Path) -> None:
    path = app_dir / RUN_CONTROL_FILE
    if path.exists():
        path.unlink()


def terminate_process_tree(proc: subprocess.Popen[Any] | None, app_dir: Path, project: Path) -> bool:
    """Terminate a tracked runner and all descendants; validate persisted metadata."""
    meta = load_json(app_dir / RUN_CONTROL_FILE, {})
    pid = proc.pid if proc is not None else int(meta.get("pid", 0) or 0)
    if not pid or str(meta.get("project", project.resolve())) != str(project.resolve()):
        return False
    if proc is None and not persisted_process_alive(app_dir, project):
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=8)
        else:
            pgid = int(meta.get("pgid", pid) or pid)
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if proc is not None and proc.poll() is not None:
                    break
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        clear_run_control(app_dir)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
