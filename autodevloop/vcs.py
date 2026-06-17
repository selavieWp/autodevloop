"""Lightweight git helpers for snapshotting each version inside current/.

Git is optional: the versions/ folder copies remain the real backup. When git
is available we additionally commit and tag every version, and place a special
tag on the version where the user's goal is first fully met.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

GOAL_TAG = "goal-complete"


def git_available() -> bool:
    return shutil.which("git") is not None


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def ensure_repo(cwd: Path) -> bool:
    """Initialise a git repo in cwd if needed. Returns True if usable."""
    if not git_available():
        return False
    cwd.mkdir(parents=True, exist_ok=True)
    if (cwd / ".git").exists():
        return True
    if _run(["init"], cwd).returncode != 0:
        return False
    # Ensure commits work even without a global identity configured.
    if not _run(["config", "user.name"], cwd).stdout.strip():
        _run(["config", "user.name", "AutoDevLoop"], cwd)
    if not _run(["config", "user.email"], cwd).stdout.strip():
        _run(["config", "user.email", "autodevloop@local"], cwd)
    _run(["config", "commit.gpgsign", "false"], cwd)
    gitignore = cwd / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("__pycache__/\nnode_modules/\n.venv/\ndist/\nbuild/\n", encoding="utf-8")
    return True


def commit_all(cwd: Path, message: str) -> str | None:
    if not (cwd / ".git").exists():
        return None
    _run(["add", "-A"], cwd)
    status = _run(["status", "--porcelain"], cwd)
    if not status.stdout.strip():
        # Nothing changed; still allow an (empty) commit so every version has one.
        result = _run(["commit", "--allow-empty", "-m", message], cwd)
    else:
        result = _run(["commit", "-m", message], cwd)
    if result.returncode != 0:
        return None
    rev = _run(["rev-parse", "--short", "HEAD"], cwd)
    return rev.stdout.strip() or None


def tag(cwd: Path, name: str, message: str = "") -> bool:
    if not (cwd / ".git").exists():
        return False
    _run(["tag", "-d", name], cwd)  # replace if exists
    result = _run(["tag", "-a", name, "-m", message or name], cwd)
    return result.returncode == 0
