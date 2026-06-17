"""Built-in test detection and execution (no LLM required)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .util import INTERNAL_DIRS, list_generated_files, load_json, read_text


def detect_candidates(base_dir: Path) -> list[dict[str, Any]]:
    files = set(list_generated_files(base_dir))
    candidates: list[dict[str, Any]] = []

    if "package.json" in files:
        package = load_json(base_dir / "package.json", {})
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if "test" in scripts:
            candidates.append({"name": "npm test", "command": "npm test", "kind": "node"})
        if "build" in scripts:
            candidates.append({"name": "npm run build", "command": "npm run build", "kind": "node"})

    has_py_tests = any(f.startswith("tests/") and f.endswith(".py") for f in files) or any(
        Path(f).name.startswith("test_") for f in files
    )
    if has_py_tests:
        candidates.append({"name": "pytest", "command": "python -m pytest -q", "kind": "python"})
        candidates.append({"name": "unittest", "command": "python -m unittest discover", "kind": "python"})

    if "index.html" in files:
        html = read_text(base_dir / "index.html")
        candidates.append({"name": "static html smoke", "command": "__builtin_html_smoke__", "kind": "web"})
        if 'type="module"' in html or "type='module'" in html:
            candidates.append({"name": "module html server smoke", "command": "__builtin_html_server_smoke__", "kind": "web"})

    if "pyproject.toml" in files or any(f.endswith(".py") for f in files):
        candidates.append({"name": "python compile", "command": "__builtin_python_compile__", "kind": "python"})

    candidates.append({"name": "file smoke", "command": "__builtin_file_smoke__", "kind": "generic"})
    return candidates


def run_command(base_dir: Path, command: str, timeout: int, log_path: Path) -> dict[str, Any]:
    builtins = {
        "__builtin_file_smoke__": _file_smoke,
        "__builtin_html_smoke__": lambda d, t: _html_smoke(d, server=False),
        "__builtin_html_server_smoke__": lambda d, t: _html_smoke(d, server=True),
        "__builtin_python_compile__": _python_compile,
    }
    if command in builtins:
        return builtins[command](base_dir, timeout)

    try:
        completed = subprocess.run(
            command, cwd=str(base_dir), shell=True, text=True, encoding="utf-8",
            errors="replace", capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"$ {command}\nTimed out after {timeout}s.\n", encoding="utf-8")
        return {"success": False, "command": command, "returncode": -1, "log": log_path.name}

    output = (
        f"$ {command}\ncwd={base_dir}\nreturncode={completed.returncode}\n\n"
        f"[stdout]\n{completed.stdout}\n\n[stderr]\n{completed.stderr}\n"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return {"success": completed.returncode == 0, "command": command, "returncode": completed.returncode, "log": log_path.name}


def _file_smoke(base_dir: Path, _timeout: int) -> dict[str, Any]:
    files = list_generated_files(base_dir)
    success = bool(files)
    return {"success": success, "command": "__builtin_file_smoke__", "returncode": 0 if success else 1, "details": f"{len(files)} files"}


def _python_compile(base_dir: Path, timeout: int) -> dict[str, Any]:
    py_files = [
        str(p) for p in base_dir.rglob("*.py")
        if not any(part in INTERNAL_DIRS for part in p.relative_to(base_dir).parts)
    ]
    if not py_files:
        return {"success": True, "command": "__builtin_python_compile__", "returncode": 0, "details": "No Python files."}
    completed = subprocess.run(
        [sys.executable, "-m", "py_compile", *py_files],
        cwd=str(base_dir), text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=timeout,
    )
    return {
        "success": completed.returncode == 0,
        "command": "__builtin_python_compile__",
        "returncode": completed.returncode,
        "details": (completed.stderr or "").strip()[:300],
    }


def _html_smoke(base_dir: Path, server: bool) -> dict[str, Any]:
    marker = "__builtin_html_server_smoke__" if server else "__builtin_html_smoke__"
    index = base_dir / "index.html"
    if not index.exists():
        return {"success": False, "command": marker, "returncode": 1, "details": "index.html missing"}
    html = read_text(index)
    missing: list[str] = []
    for attr in re.findall(r"""(?:src|href)=["']([^"']+)["']""", html):
        if attr.startswith(("http://", "https://", "data:", "#", "//", "mailto:")):
            continue
        target = base_dir / attr.split("?", 1)[0].lstrip("/")
        if not target.exists():
            missing.append(attr)
    module = 'type="module"' in html or "type='module'" in html
    if missing:
        return {"success": False, "command": marker, "returncode": 1, "details": f"Missing assets: {missing[:5]}"}
    if module and not server:
        return {"success": False, "command": "__builtin_html_smoke__", "returncode": 1, "details": "Uses ES modules; needs a local server."}
    return {"success": True, "command": marker, "returncode": 0, "details": "HTML assets resolved."}
