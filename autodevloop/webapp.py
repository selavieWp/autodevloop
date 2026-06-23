"""A dependency-free local web dashboard for AutoDevLoop.

Bind to localhost only. It can create, start, and stop runs, exposes live run
state (progress, per-agent timers, token usage, per-agent output), and an
editable settings page (config + prompt templates) that is locked while a run
is active. It never asks for API keys; provider switching is a CLI command swap.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import prompts, registry, control, repair
from .config import deep_merge, load_config, save_config, deep_get
from .util import (
    APP_DIR, PROGRESS_FILE, STATE_FILE, STOP_FILE,
    load_json, now_text, read_text, restore_working_dir, rmtree_robust,
    save_json, write_text,
)

_RUNS: dict[str, subprocess.Popen] = {}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, text: str,
                   content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _safe_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Run process tracking
# --------------------------------------------------------------------------- #
def _is_running(dir_str: str) -> bool:
    root = Path(dir_str).resolve()
    with _LOCK:
        proc = _RUNS.get(str(root))
    return (proc is not None and proc.poll() is None) or control.persisted_process_alive(root / APP_DIR, root)


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
    except Exception:  # noqa: BLE001
        pass


def _watch_run(root: Path, proc: subprocess.Popen, log_file: Any) -> None:
    proc.wait()
    try:
        log_file.close()
    except OSError:
        pass
    with _LOCK:
        if _RUNS.get(str(root)) is proc:
            _RUNS.pop(str(root), None)
    meta = load_json(root / APP_DIR / control.RUN_CONTROL_FILE, {})
    if int(meta.get("pid", 0) or 0) == proc.pid:
        control.clear_run_control(root / APP_DIR)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Project summaries / config
# --------------------------------------------------------------------------- #
def _project_summary(entry: dict[str, Any]) -> dict[str, Any]:
    root = Path(entry["dir"])
    state = load_json(root / APP_DIR / STATE_FILE, {})
    config = load_config(root)
    running = _is_running(str(root))
    status = "running" if running else (state.get("status") or "initialized")
    return {
        "dir": str(root),
        "name": entry.get("name") or deep_get(config, "project.name", "") or root.name,
        "status": status,
        "phase": state.get("phase", "build"),
        "current_version": state.get("current_version", 0),
        "max_versions": state.get("max_versions") or deep_get(config, "project.max_versions", 0),
        "goal_progress": state.get("goal_progress", 0),
        "running": running,
    }


def _create_project(payload: dict[str, Any]) -> dict[str, Any]:
    raw_dir = str(payload.get("dir") or "").strip()
    goal = str(payload.get("goal") or "").strip()
    if not raw_dir:
        return {"ok": False, "error": "project directory is required"}
    if not goal:
        return {"ok": False, "error": "goal is required"}
    root = Path(raw_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    config = load_config(root)
    overrides: dict[str, Any] = {"project": {"goal": goal}, "provider": {}, "pipeline": {}}
    if payload.get("name"):
        overrides["project"]["name"] = payload["name"]
    if payload.get("max_versions"):
        overrides["project"]["max_versions"] = int(payload["max_versions"])
    if payload.get("arch_hint"):
        overrides["project"]["arch_hint"] = payload["arch_hint"]
    if payload.get("mode"):
        overrides["pipeline"]["mode"] = payload["mode"]
    if payload.get("provider"):
        overrides["provider"]["name"] = payload["provider"]
    if payload.get("provider_command"):
        overrides["provider"]["command"] = payload["provider_command"]
    if payload.get("model"):
        overrides["provider"]["model"] = payload["model"]
    overrides["project"]["brainstorm"] = bool(payload.get("brainstorm"))
    save_config(root, deep_merge(config, {k: v for k, v in overrides.items() if v}))
    (root / APP_DIR).mkdir(parents=True, exist_ok=True)
    prompts.ensure_templates(root / APP_DIR)
    registry.register(root, str(payload.get("name") or root.name))
    return {"ok": True, "dir": str(root), "brainstorm": bool(payload.get("brainstorm"))}


def _delete_project(payload: dict[str, Any]) -> dict[str, Any]:
    """Delete one registered project directory after an exact-path confirmation."""
    raw_dir = str(payload.get("dir") or "").strip()
    if not raw_dir:
        return {"ok": False, "error": "project directory is required"}
    root = Path(raw_dir).expanduser().resolve()
    if str(payload.get("confirm_dir") or "") != str(root):
        return {"ok": False, "error": "project directory confirmation did not match"}

    registered = any(
        str(entry.get("dir") or "").strip()
        and Path(str(entry["dir"])).expanduser().resolve() == root
        for entry in registry.load()
    )
    if not registered:
        return {"ok": False, "error": "refusing to delete an unregistered project"}
    if _is_running(str(root)):
        return {"ok": False, "error": "cannot delete a project while a run is active"}

    package_root = Path(__file__).resolve().parents[1]
    protected = {Path(root.anchor).resolve(), Path.home().resolve(), package_root}
    if root in protected:
        return {"ok": False, "error": f"refusing to delete protected directory: {root}"}
    if root.exists() and not root.is_dir():
        return {"ok": False, "error": "project path is not a directory"}

    existed = root.exists()
    if existed:
        try:
            rmtree_robust(root)
        except OSError as exc:
            return {"ok": False, "error": f"failed to delete project directory: {exc}"}
    registry.remove(root)
    with _LOCK:
        _RUNS.pop(str(root), None)
    return {"ok": True, "dir": str(root), "deleted": existed}


def _brainstorm_turn(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one brainstorming turn for the web UI (1 request == 1 LLM turn).

    The client sends the user's latest ``reply`` (empty on the first call); the
    server records it, asks the model for the next question, and persists the
    transcript to ``.autodev/brainstorm.json``. When the design is ready, the
    refined goal + arch hint are written back into the project config.
    """
    from . import brainstorm
    from .config import provider_for_agent
    raw_dir = str(payload.get("dir") or "").strip()
    if not raw_dir:
        return {"ok": False, "error": "dir required"}
    root = Path(raw_dir).expanduser().resolve()
    app_dir = root / APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(root)
    goal = str(payload.get("goal") or deep_get(config, "project.goal", "")).strip()
    session = brainstorm.load_session(app_dir, goal)
    session["goal"] = session.get("goal") or goal

    reply_text = str(payload.get("reply") or "").strip()
    if reply_text:
        brainstorm.record_reply(app_dir, session, reply_text)
    try:
        result = brainstorm.next_turn(provider_for_agent(config, "brainstorm"), root, app_dir, session)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    if result.get("done"):
        refined_goal, spec, arch_hint = brainstorm.finalize(root, session)
        # The agreed document is the actual build brief consumed by every
        # downstream agent. The shorter refined goal remains in the session.
        merged: dict[str, Any] = {"project": {"goal": spec or refined_goal}}
        if arch_hint:
            existing = deep_get(config, "project.arch_hint", "")
            merged["project"]["arch_hint"] = f"{existing}\n{arch_hint}".strip() if existing else arch_hint
        save_config(root, deep_merge(config, merged))
        return {"ok": True, "done": True, "refined_goal": refined_goal, "spec": spec}
    return {"ok": True, "done": False, "question": result.get("question", ""),
            "choices": result.get("choices") or [], "turn": session.get("turns", 0)}


def _brainstorm_design(dir_str: str) -> dict[str, Any]:
    """Return the persisted design, history and whether the design is editable."""
    from . import brainstorm

    root = Path(dir_str).expanduser().resolve()
    app_dir = root / APP_DIR
    session = brainstorm.load_session(app_dir)
    spec = str(session.get("spec") or "").strip()
    if not spec:
        spec = read_text(root / "docs" / brainstorm.BRAINSTORM_SPEC_FILE).strip()
    history_path = root / "docs" / brainstorm.BRAINSTORM_HISTORY_FILE
    history = read_text(history_path).strip()
    # Backfill history for brainstorms completed before history documents were
    # introduced. The JSON transcript already contains everything we need.
    if not history:
        rendered_history = brainstorm.render_history(session)
        if rendered_history:
            write_text(history_path, rendered_history)
            history = rendered_history.strip()
    state = load_json(app_dir / STATE_FILE, {})
    progress = load_json(app_dir / PROGRESS_FILE, {})
    has_run = bool(state) or bool(progress.get("run_started_at"))
    return {
        "ok": True,
        "exists": bool(spec),
        "spec": spec,
        "history": history,
        "editable": bool(spec) and not has_run and not _is_running(str(root)),
        "has_run": has_run,
    }


def _save_brainstorm_design(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a user-edited final design before the first project run."""
    from . import brainstorm

    raw_dir = str(payload.get("dir") or "").strip()
    spec = str(payload.get("spec") or "").strip()
    if not raw_dir:
        return {"ok": False, "error": "project directory is required"}
    if not spec:
        return {"ok": False, "error": "design cannot be empty"}
    root = Path(raw_dir).expanduser().resolve()
    design = _brainstorm_design(str(root))
    if not design.get("exists"):
        return {"ok": False, "error": "no completed brainstorm design exists"}
    if design.get("has_run") or _is_running(str(root)):
        return {"ok": False, "error": "the design is read-only after the project has started"}

    app_dir = root / APP_DIR
    session = brainstorm.load_session(app_dir, deep_get(load_config(root), "project.goal", ""))
    if not session.get("generated_spec"):
        session["generated_spec"] = str(session.get("spec") or "").strip()
    session["spec"] = spec
    session["done"] = True
    brainstorm.save_session(app_dir, session)
    write_text(root / "docs" / brainstorm.BRAINSTORM_SPEC_FILE, spec + "\n")
    history_path = root / "docs" / brainstorm.BRAINSTORM_HISTORY_FILE
    if not history_path.exists():
        history = brainstorm.render_history(session)
        if history:
            write_text(history_path, history)

    config = load_config(root)
    save_config(root, deep_merge(config, {"project": {"goal": spec}}))
    return {"ok": True, "spec": spec, "editable": True}


def _start_run(payload: dict[str, Any]) -> dict[str, Any]:
    raw_dir = str(payload.get("dir") or "").strip()
    if not raw_dir:
        return {"ok": False, "error": "project directory is required"}
    root = Path(raw_dir).expanduser().resolve()
    if _is_running(str(root)):
        return {"ok": False, "error": "a run is already active for this project"}

    config = load_config(root)
    state = load_json(root / APP_DIR / STATE_FILE, {})
    goal = deep_get(config, "project.goal", "") or state.get("goal", "")
    if not goal:
        return {"ok": False, "error": "no goal configured; create the project first"}

    cmd = [sys.executable, "-u", "-m", "autodevloop", "run", "--project-dir", str(root), "--non-interactive"]
    if payload.get("reset"):
        cmd += ["--reset"]

    (root / APP_DIR).mkdir(parents=True, exist_ok=True)
    log_file = (root / APP_DIR / "web_run.log").open("a", encoding="utf-8")
    log_file.write(f"\n=== run started {now_text()} ===\n")
    log_file.flush()
    pkg_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    popen_opts: dict[str, Any] = {}
    if os.name == "nt":
        popen_opts["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_opts["start_new_session"] = True
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, cwd=pkg_root, env=env, **popen_opts)
    control.write_run_control(root / APP_DIR, proc.pid, root)
    with _LOCK:
        _RUNS[str(root)] = proc
    threading.Thread(target=_watch_run, args=(root, proc, log_file), daemon=True).start()
    return {"ok": True, "dir": str(root), "pid": proc.pid}


def _pause_run(payload: dict[str, Any]) -> dict[str, Any]:
    raw_dir = str(payload.get("dir") or "").strip()
    if not raw_dir:
        return {"ok": False, "error": "dir required"}
    root = Path(raw_dir).expanduser().resolve()
    app_dir = root / APP_DIR
    with _LOCK:
        proc = _RUNS.pop(str(root), None)
    killed = control.terminate_process_tree(proc, app_dir, root)
    cp = control.load_checkpoint(app_dir)
    if not cp:
        return {"ok": False, "error": "no resumable checkpoint"}
    target = Path(cp.get("working_dir")) if cp.get("run_type") == "repair" and cp.get("working_dir") else root / "current"
    control.restore_active(app_dir, target)
    cp["status"] = "paused"
    cp["pause_reason"] = "Paused by user; in-flight agent output was discarded"
    control.save_checkpoint(app_dir, cp)
    state = load_json(app_dir / STATE_FILE, {})
    if cp.get("run_type") == "repair":
        job = repair.load_job(root, str(cp.get("job_id") or ""))
        if job:
            job["status"] = "paused"
            job["error"] = cp["pause_reason"]
            repair.save_job(root, job)
    else:
        state["status"] = "paused"
        state["stop_reason"] = cp["pause_reason"]
        save_json(app_dir / STATE_FILE, state)
        control.write_progress_doc(root, cp, state)
    prog = load_json(app_dir / PROGRESS_FILE, {})
    prog.update({"status": "paused", "active": [], "run_ended_at": now_text()})
    prog.setdefault("events", []).append({
        "time": now_text(), "step": "PAUSE", "agent": cp.get("next_agent", ""),
        "message": "in-flight output discarded; checkpoint preserved", "kind": "discarded",
    })
    save_json(app_dir / PROGRESS_FILE, prog, stamp=False)
    return {"ok": True, "mode": "pause", "killed": killed, "checkpoint": cp}


def _start_repair(payload: dict[str, Any], existing_job_id: str = "") -> dict[str, Any]:
    raw_dir = str(payload.get("dir") or "").strip()
    if not raw_dir:
        return {"ok": False, "error": "dir required"}
    root = Path(raw_dir).expanduser().resolve()
    if _is_running(str(root)):
        return {"ok": False, "error": "another project task is already active"}
    try:
        job = repair.load_job(root, existing_job_id) if existing_job_id else repair.create_job(
            root, int(payload.get("version", 0) or 0), str(payload.get("request") or ""),
            str(payload.get("test_command") or ""),
        )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    if not job:
        return {"ok": False, "error": "repair job not found"}
    cmd = [sys.executable, "-u", "-m", "autodevloop", "repair-run", "--project-dir", str(root), "--job-id", job["id"]]
    log_file = (root / APP_DIR / "web_run.log").open("a", encoding="utf-8")
    pkg_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    opts: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, cwd=pkg_root, env=env, **opts)
    control.write_run_control(root / APP_DIR, proc.pid, root)
    with _LOCK:
        _RUNS[str(root)] = proc
    threading.Thread(target=_watch_run, args=(root, proc, log_file), daemon=True).start()
    return {"ok": True, "job": job, "pid": proc.pid}


def _resume_run(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(str(payload.get("dir") or "")).expanduser().resolve()
    cp = control.load_checkpoint(root / APP_DIR)
    if cp.get("run_type") == "repair":
        return _start_repair(payload, str(cp.get("job_id") or ""))
    return _start_run(payload)


def _promote_repair(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(str(payload.get("dir") or "")).expanduser().resolve()
    if _is_running(str(root)) or control.load_checkpoint(root / APP_DIR).get("status") == "paused":
        return {"ok": False, "error": "stop or finish the active task before promotion"}
    return repair.promote_job(root, str(payload.get("job_id") or ""))


def _rollback_current(root: Path) -> tuple[bool, int]:
    """Restore current/ to the last completed version. Returns (rolled, version)."""
    state = load_json(root / APP_DIR / STATE_FILE, {})
    cur = int(state.get("current_version", 0) or 0)
    target = root / "current"
    before = root / APP_DIR / "work" / f"v{cur + 1}" / "_before"
    if before.exists():
        restore_working_dir(before, target)
        return True, cur
    snap = root / "versions" / f"v{cur}"
    if cur > 0 and snap.exists():
        restore_working_dir(snap, target)
        return True, cur
    return False, cur


def _stop_run(payload: dict[str, Any]) -> dict[str, Any]:
    raw_dir = str(payload.get("dir") or "").strip()
    mode = str(payload.get("mode") or "graceful")
    if not raw_dir:
        return {"ok": False, "error": "dir required"}
    root = Path(raw_dir).expanduser().resolve()
    app_dir = root / APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)

    if mode == "immediate":
        existing_cp = control.load_checkpoint(app_dir)
        if existing_cp.get("run_type") == "repair":
            return {"ok": False, "error": "discard-version stop applies only to the main development flow; pause the repair instead"}
        with _LOCK:
            proc = _RUNS.pop(str(root), None)
        persisted_alive = control.persisted_process_alive(app_dir, root)
        has_checkpoint = bool(control.load_checkpoint(app_dir))
        if (proc is None or proc.poll() is not None) and not persisted_alive and not has_checkpoint:
            # No validated process remains; leave a STOP marker for a runner
            # that may be between startup and metadata persistence.
            write_text(app_dir / STOP_FILE, f"stop requested at {now_text()}\n")
            return {"ok": True, "mode": "graceful_fallback",
                    "note": "process not tracked; requested graceful stop instead"}
        control.terminate_process_tree(proc, app_dir, root)
        rolled, rolled_to = _rollback_current(root)
        control.clear_checkpoint(app_dir)
        # Patch state + progress so the UI reflects the abort.
        state = load_json(app_dir / STATE_FILE, {})
        if state:
            state["status"] = "stopped"
            state["stop_reason"] = "Immediate stop: current version discarded and rolled back"
            save_json(app_dir / STATE_FILE, state)
        prog = load_json(app_dir / PROGRESS_FILE, {})
        if prog:
            prog["status"] = "stopped"
            prog["active"] = []
            prog["run_ended_at"] = now_text()
            prog.setdefault("events", []).append(
                {"time": now_text(), "step": "STOP", "agent": "", "message": "immediate stop; version discarded"})
            save_json(app_dir / PROGRESS_FILE, prog, stamp=False)
        return {"ok": True, "mode": "immediate", "rolled_back": rolled, "rolled_to": rolled_to}

    # graceful
    write_text(app_dir / STOP_FILE, f"stop requested at {now_text()}\n")
    return {"ok": True, "mode": "graceful"}


def _get_config(dir_str: str) -> dict[str, Any]:
    root = Path(dir_str).expanduser().resolve()
    config = load_config(root)
    app_dir = root / APP_DIR
    prompts.ensure_templates(app_dir)
    templates = {name: prompts.load_template(app_dir, name) for name in prompts.TEMPLATE_NAMES}
    return {"config": config, "templates": templates,
            "template_names": prompts.TEMPLATE_NAMES,
            "required_tokens": prompts.REQUIRED_TOKENS,
            "running": _is_running(str(root))}


def _save_config(dir_str: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(dir_str).expanduser().resolve()
    if _is_running(str(root)):
        return {"ok": False, "error": "cannot edit settings while a run is active"}
    templates = payload.get("templates") or {}
    # Refuse to save prompts that dropped tokens the engine depends on, so an
    # edited prompt can't silently break the pipeline.
    invalid: dict[str, list[str]] = {}
    for name, body in templates.items():
        if name in prompts.TEMPLATE_NAMES and isinstance(body, str):
            missing = prompts.validate_template(name, body)
            if missing:
                invalid[name] = missing
    if invalid:
        return {"ok": False, "error": "invalid_templates", "invalid": invalid}

    config = payload.get("config")
    if isinstance(config, dict):
        # Settings UI sends only editable fields; preserve the project goal,
        # architecture hint, brainstorm flag, and any future unknown keys.
        save_config(root, deep_merge(load_config(root), config))
    base = prompts.templates_dir(root / APP_DIR)
    base.mkdir(parents=True, exist_ok=True)
    for name, body in templates.items():
        if name in prompts.TEMPLATE_NAMES and isinstance(body, str):
            write_text(base / f"{name}.md", body.rstrip() + "\n")
    return {"ok": True}


def _directives_payload(dir_str: str) -> dict[str, Any]:
    root = Path(dir_str).expanduser().resolve()
    state = load_json(root / APP_DIR / STATE_FILE, {})
    return {"directives": control.load_directives(root / APP_DIR),
            "version": int(state.get("current_version", 0) or 0) + 1}


def _save_directive(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(str(payload.get("dir") or "")).expanduser().resolve()
    app_dir = root / APP_DIR
    state = load_json(app_dir / STATE_FILE, {})
    if payload.get("id"):
        ok = control.set_directive_active(app_dir, str(payload["id"]), bool(payload.get("active")))
        return {"ok": ok}
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "directive text is required"}
    cp = control.load_checkpoint(app_dir)
    version = int(cp["version"]) if "version" in cp else int(state.get("current_version", 0) or 0) + 1
    return {"ok": True, "directive": control.add_directive(app_dir, text, str(payload.get("scope") or "version"), version)}


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return _text_response(self, INDEX_HTML, "text/html; charset=utf-8")
        if path == "/api/projects":
            return _json_response(self, {"projects": [_project_summary(e) for e in registry.load()]})
        if path == "/api/brainstorm/design":
            return _json_response(self, _brainstorm_design(self._query().get("dir", "")))
        if path == "/api/state":
            root = Path(self._query().get("dir", ""))
            return _json_response(self, load_json(root / APP_DIR / STATE_FILE, {}))
        if path == "/api/progress":
            root = Path(self._query().get("dir", ""))
            prog = load_json(root / APP_DIR / PROGRESS_FILE, {})
            if isinstance(prog, dict):
                prog["running"] = _is_running(str(root))
            return _json_response(self, prog)
        if path == "/api/config":
            return _json_response(self, _get_config(self._query().get("dir", "")))
        if path == "/api/checkpoint":
            root = Path(self._query().get("dir", "")).resolve()
            return _json_response(self, control.load_checkpoint(root / APP_DIR))
        if path == "/api/directives":
            return _json_response(self, _directives_payload(self._query().get("dir", "")))
        if path == "/api/repairs":
            root = Path(self._query().get("dir", "")).resolve()
            return _json_response(self, {"jobs": repair.list_jobs(root)})
        if path == "/api/log":
            return self._serve_log()
        if path == "/api/doc":
            return self._serve_doc()
        return _text_response(self, "not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_body()
        if path == "/api/create":
            return _json_response(self, _create_project(body))
        if path == "/api/delete":
            return _json_response(self, _delete_project(body))
        if path == "/api/brainstorm":
            return _json_response(self, _brainstorm_turn(body))
        if path == "/api/brainstorm/design":
            return _json_response(self, _save_brainstorm_design(body))
        if path == "/api/start":
            return _json_response(self, _start_run(body))
        if path == "/api/resume":
            return _json_response(self, _resume_run(body))
        if path == "/api/pause":
            return _json_response(self, _pause_run(body))
        if path == "/api/repairs/start":
            return _json_response(self, _start_repair(body))
        if path == "/api/repairs/promote":
            return _json_response(self, _promote_repair(body))
        if path == "/api/stop":
            return _json_response(self, _stop_run(body))
        if path == "/api/config":
            return _json_response(self, _save_config(self._query().get("dir", ""), body))
        if path == "/api/directives":
            return _json_response(self, _save_directive(body))
        return _text_response(self, "not found", status=404)

    def _serve_log(self) -> None:
        q = self._query()
        root = Path(q.get("dir", "")).resolve()
        name = q.get("name", "")
        target = root / APP_DIR / "logs" / name
        if not name or not _safe_within(root / APP_DIR / "logs", target) or not target.exists():
            return _text_response(self, "(log not found)")
        return _text_response(self, read_text(target))

    def _serve_doc(self) -> None:
        q = self._query()
        root = Path(q.get("dir", "")).resolve()
        which = q.get("name", "changelog")
        mapping = {"changelog": root / "CHANGELOG.md", "features": root / "FEATURES.md",
                   "brainstorm-spec": root / "docs" / "brainstorm-spec.md",
                   "brainstorm-history": root / "docs" / "brainstorm-history.md",
                   "development-progress": root / "docs" / "development-progress.md",
                   "report": root / APP_DIR / "final_report.md", "weblog": root / APP_DIR / "web_run.log"}
        target = mapping.get(which)
        if target is None or not target.exists():
            return _text_response(self, "(not available yet)")
        return _text_response(self, read_text(target))


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[AutoDevLoop] Web dashboard: http://{host}:{port}")
    print("[AutoDevLoop] Press Ctrl+C to stop the server (running projects keep going).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[AutoDevLoop] Web server stopped.")
    finally:
        server.server_close()


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoDevLoop</title>
<style>
:root{
  --bg:#f4f6fb; --panel:#ffffff; --panel2:#f0f3f9; --line:#e2e7f0; --line2:#d4dbe8;
  --text:#1f2733; --muted:#6b7686; --brand:#3b6fe0; --brand-soft:#e8f0ff;
  --ok:#1f9d57; --ok-soft:#e3f6ec; --bad:#d8472b; --bad-soft:#fdeae6;
  --warn:#c07a13; --warn-soft:#fdf2dd; --run:#3b6fe0; --run-soft:#e8f0ff;
  --shadow:0 1px 3px rgba(20,30,60,.07),0 1px 2px rgba(20,30,60,.04);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:14px;display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{display:flex;align-items:center;gap:10px;padding:10px 18px;background:var(--panel);border-bottom:1px solid var(--line);box-shadow:var(--shadow);flex-wrap:nowrap;flex-shrink:0}
header .brand{font-size:16px;font-weight:700;letter-spacing:.2px;white-space:nowrap}
header .brand b{color:var(--brand)}
header .sub{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
header .spacer{flex:1}
button{background:var(--brand);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:600;transition:.15s}
button:hover{filter:brightness(.96)}
button.sec{background:var(--panel2);border:1px solid var(--line2);color:var(--text)}
button.ok{background:var(--ok)}
button.warn{background:var(--warn)}
button.danger{background:var(--bad)}
button:disabled{opacity:.45;cursor:not-allowed;filter:none}
#newBtn{white-space:nowrap}
.iconbtn{background:var(--panel2);border:1px solid var(--line2);color:var(--text);border-radius:8px;padding:7px 10px;font-weight:600;font-size:13px;white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.iconbtn:hover{background:var(--brand-soft);border-color:var(--brand)}
.langwrap{position:relative}
.menu{position:absolute;right:0;top:calc(100% + 6px);background:var(--panel);border:1px solid var(--line2);border-radius:10px;box-shadow:0 12px 30px rgba(20,30,60,.18);min-width:140px;z-index:30;display:none;overflow:hidden}
.menu.show{display:block}
.menu div{padding:9px 14px;cursor:pointer;font-size:13px}
.menu div:hover{background:var(--brand-soft)}
.menu div.sel{color:var(--brand);font-weight:700}
.layout{display:flex;flex:1;min-height:0}
.sidebar{width:264px;background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:14px}
.sidebar .h{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:10px}
.main{flex:1;overflow:auto;padding:20px 24px}
.proj{padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-bottom:9px;cursor:pointer;background:var(--panel)}
.proj:hover{border-color:var(--line2)}
.proj.active{border-color:var(--brand);background:var(--brand-soft)}
.proj .head{display:flex;align-items:center;gap:8px}
.proj .nm{font-weight:600}
.proj .del{margin-left:auto;background:transparent;color:var(--muted);border:0;padding:2px 5px;border-radius:6px;font-size:14px;line-height:1}
.proj .del:hover{background:var(--bad-soft);color:var(--bad)}
.proj .meta{color:var(--muted);font-size:12px;margin-top:4px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.tabs{display:flex;gap:6px;margin-bottom:18px}
.tabs .t{padding:8px 14px;border-radius:9px;cursor:pointer;color:var(--muted);font-weight:600}
.tabs .t:hover{background:var(--panel2)}
.tabs .t.active{background:var(--brand);color:#fff}
.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap}
.toolbar .rt{margin-left:auto;color:var(--muted);font-size:13px}
.toolbar .rt b{color:var(--text);font-variant-numeric:tabular-nums}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:var(--shadow)}
.card .lbl{color:var(--muted);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
.card .val{font-size:22px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums}
.card .sub{color:var(--muted);font-size:12px;margin-top:4px}
.bar{height:7px;background:var(--panel2);border-radius:6px;overflow:hidden;margin-top:9px}
.bar>i{display:block;height:100%;background:var(--brand);transition:width .4s}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px;box-shadow:var(--shadow)}
.panel h3{margin:0 0 12px;font-size:14px;display:flex;align-items:center;gap:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}
.vtable{border:1px solid var(--line);border-radius:10px;overflow:hidden}
.vtable th{white-space:nowrap;background:var(--panel2);border-bottom:1px solid var(--line2)}
.vtable td.nowrap,.vtable th{white-space:nowrap}
.vtable tbody tr:nth-child(even){background:var(--panel2)}
.vtable tbody tr:hover{background:var(--brand-soft)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:7px;font-size:12px;font-weight:700;background:var(--panel2);color:var(--muted);border:1px solid var(--line2)}
.badge.ok{color:var(--ok);background:var(--ok-soft);border-color:transparent}
.badge.bad{color:var(--bad);background:var(--bad-soft);border-color:transparent}
.badge.warn{color:var(--warn);background:var(--warn-soft);border-color:transparent}
.pill{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;border:1px solid var(--line2);background:var(--panel2);color:var(--muted)}
.pill.ok{color:var(--ok);background:var(--ok-soft);border-color:transparent}
.pill.bad{color:var(--bad);background:var(--bad-soft);border-color:transparent}
.pill.run{color:var(--run);background:var(--run-soft);border-color:transparent}
.pill.warn{color:var(--warn);background:var(--warn-soft);border-color:transparent}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.run .dot{animation:blink 1s infinite}
@keyframes blink{50%{opacity:.3}}
.agents{display:flex;flex-direction:column;gap:8px}
.agent-row{display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}
.agent-row .nm{font-weight:600}
.agent-row .st{color:var(--muted);font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.agent-row .tm{font-variant-numeric:tabular-nums;color:var(--brand);font-weight:700}
.events{max-height:360px;overflow:auto;display:flex;flex-direction:column;gap:2px}
.ev{padding:6px 8px;border-radius:8px;font-size:12.5px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.ev:hover{background:var(--panel2)}
.ev .tm{color:var(--muted);font-variant-numeric:tabular-nums;font-size:11px}
.ev .stp{font-weight:700;color:var(--brand)}
.ev .ag{color:var(--warn);font-weight:600}
.ev .msg{color:var(--text)}
.ev .snip{flex-basis:100%;color:var(--muted);margin:2px 0 0 0;padding-left:8px;border-left:2px solid var(--line2)}
.ev-div{display:flex;align-items:center;gap:10px;margin:8px 2px;color:var(--brand);font-weight:700;font-size:12px}
.ev-div:before,.ev-div:after{content:"";flex:1;height:2px;background:linear-gradient(90deg,transparent,var(--brand-soft),var(--brand))}
.ev-div:after{background:linear-gradient(90deg,var(--brand),var(--brand-soft),transparent)}
.logbtn{background:var(--brand-soft);color:var(--brand);border:1px solid var(--brand);border-radius:7px;padding:2px 9px;font-size:11.5px;font-weight:700;cursor:pointer}
.logbtn:hover{background:var(--brand);color:#fff}
pre{white-space:pre-wrap;background:var(--panel2);padding:12px;border-radius:10px;max-height:380px;overflow:auto;font-size:12.5px;font-family:ui-monospace,Consolas,monospace;margin:0}
.viewer-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
input,select,textarea{width:100%;background:var(--panel);border:1px solid var(--line2);color:var(--text);border-radius:9px;padding:9px;font-size:13px;font-family:inherit}
input:disabled,select:disabled,textarea:disabled{background:var(--panel2);color:var(--muted)}
textarea{min-height:150px;font-family:ui-monospace,Consolas,monospace}
label{display:block;margin:11px 0 5px;color:var(--muted);font-size:12px;font-weight:600}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.banner{padding:10px 12px;border-radius:10px;background:var(--warn-soft);color:var(--warn);font-weight:600;margin-bottom:12px}
.modal{position:fixed;inset:0;background:rgba(20,30,60,.45);display:none;align-items:center;justify-content:center;z-index:40}
.modal.show{display:flex}
.modal .box{background:var(--panel);border-radius:16px;padding:22px;width:580px;max-height:92vh;overflow:auto;box-shadow:0 20px 60px rgba(20,30,60,.25)}
.modal .box.wide{width:760px}
.modal h3{margin:0 0 6px}
.muted{color:var(--muted)}
.hint{font-size:12px;color:var(--muted);margin-top:7px}
.help{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:50%;background:var(--panel2);border:1px solid var(--line2);color:var(--muted);font-size:10px;font-weight:700;cursor:help;margin-left:5px;vertical-align:middle}
.help:hover{background:var(--brand);color:#fff;border-color:var(--brand)}
.tip{position:fixed;z-index:90;max-width:340px;background:#1f2733;color:#fff;padding:8px 11px;border-radius:8px;font-size:12px;line-height:1.5;box-shadow:0 8px 24px rgba(0,0,0,.25);display:none;pointer-events:none}
.tmpl-tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.tmpl-tabs span{padding:5px 10px;border:1px solid var(--line2);border-radius:8px;cursor:pointer;font-size:12px;font-weight:600;color:var(--muted)}
.tmpl-tabs span.active{border-color:var(--brand);color:var(--brand);background:var(--brand-soft)}
.tmpl-tabs span.bad{border-color:var(--bad);color:var(--bad)}
.tmpl-tabs span.inactive{opacity:.4}
.linklike{color:var(--brand);cursor:pointer;text-decoration:underline;font-size:12px}
.tmplnote{font-size:12px;color:var(--warn);margin-top:6px;display:none}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.chip{font-family:ui-monospace,Consolas,monospace;font-size:11px;padding:2px 7px;border-radius:6px;background:var(--panel2);border:1px solid var(--line2);color:var(--text)}
.chip.miss{background:var(--bad-soft);border-color:transparent;color:var(--bad)}
.steptable{border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-top:6px}
.grouphdr{background:var(--panel2);padding:7px 12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);border-bottom:1px solid var(--line)}
.step-row{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line)}
.step-row:last-child{border-bottom:0}
.step-row input{width:auto;margin:0}
.step-row .sa{font-weight:700;min-width:148px}
.step-row .sd{color:var(--muted);font-size:12px;flex:1}
.step-row .stmpl{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--brand);background:var(--brand-soft);padding:2px 7px;border-radius:6px;white-space:nowrap}
.step-row.req{background:var(--panel2)}
.step-row.req .sa{color:var(--muted)}
.step-row.off{opacity:.45}
.lockicon{font-size:11px;color:var(--muted)}
.helpsec{margin:14px 0}
.helpsec h4{margin:0 0 6px;font-size:14px;color:var(--brand)}
.helpsec p{margin:4px 0;color:var(--text);font-size:13px;line-height:1.6}
.helpsec table{margin-top:8px}
.helpsec td,.helpsec th{font-size:12.5px}
#bsModal .box{width:680px;padding:24px}
.bs-log{max-height:48vh;overflow:auto;padding:14px 10px 14px 4px;display:flex;flex-direction:column;gap:18px;scroll-behavior:smooth}
.bs-msg{display:flex;align-items:flex-start;gap:10px}
.bs-msg.user{flex-direction:row-reverse}
.bs-avatar{width:28px;height:28px;border-radius:9px;display:flex;align-items:center;justify-content:center;flex:0 0 28px;font-size:12px;font-weight:800;background:var(--brand-soft);color:var(--brand)}
.bs-msg.user .bs-avatar{background:#e9edf4;color:#566174}
.bs-content{max-width:82%;min-width:0}
.bs-who{font-size:11px;font-weight:700;color:var(--muted);margin:0 2px 5px}
.bs-msg.user .bs-who{text-align:right}
.bs-bubble{padding:10px 13px;border-radius:5px 14px 14px 14px;background:var(--brand-soft);line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}
.bs-msg.user .bs-bubble{background:#eef1f6;border-radius:14px 5px 14px 14px}
.bs-thinking .bs-bubble{display:flex;align-items:center;gap:10px;color:var(--muted);padding:9px 13px}
.bs-spark{width:18px;height:18px;border-radius:50%;background:conic-gradient(from 20deg,var(--brand),#9bb7ff,#c9a8ff,var(--brand));position:relative;animation:bs-spin 1.8s linear infinite}
.bs-spark:after{content:"";position:absolute;inset:4px;border-radius:50%;background:var(--brand-soft)}
.bs-dots{display:inline-flex;gap:4px;align-items:center}
.bs-dots i{width:5px;height:5px;border-radius:50%;background:var(--brand);animation:bs-dot 1.2s ease-in-out infinite}
.bs-dots i:nth-child(2){animation-delay:.16s}.bs-dots i:nth-child(3){animation-delay:.32s}
@keyframes bs-spin{to{transform:rotate(360deg)}}
@keyframes bs-dot{0%,70%,100%{opacity:.25;transform:translateY(0)}35%{opacity:1;transform:translateY(-3px)}}
.bs-choices{display:flex;flex-wrap:wrap;gap:8px;margin:-8px 0 0 38px}
.bs-choices button{background:var(--panel);color:var(--text);border:1px solid var(--line2);text-align:left;font-weight:600;box-shadow:0 1px 2px rgba(20,30,60,.04)}
.bs-choices button:hover{border-color:var(--brand);background:var(--brand-soft);color:var(--brand)}
.bs-composer{border-top:1px solid var(--line);padding-top:14px}
.bs-composer textarea{min-height:72px;resize:vertical}
.bs-final{margin-top:12px;padding:16px;border:1px solid #bfd0fa;border-radius:14px;background:linear-gradient(145deg,#f8faff,#eef4ff)}
.bs-final h4{margin:0 0 5px;font-size:14px}
.bs-final textarea{min-height:260px;margin-top:12px;background:#fff;line-height:1.55}
.bs-final-actions,.design-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-top:10px}
.design-panel{border-color:#b9ccf8;background:linear-gradient(145deg,#fff,#f4f7ff)}
.design-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:10px}
.design-head h3{margin:0 0 4px}
.design-panel textarea{min-height:280px;line-height:1.6;background:#fff}
.design-readonly{max-height:360px;line-height:1.6;background:#fff;border:1px solid var(--line)}
.save-state{font-size:12px;color:var(--ok);margin-right:auto}
@media (prefers-reduced-motion:reduce){.bs-spark,.bs-dots i{animation:none}}
</style>
</head>
<body>
<header>
  <div class="brand">Auto<b>Dev</b>Loop</div>
  <div class="sub" id="brandSub"></div>
  <div class="spacer"></div>
  <button class="iconbtn" id="helpBtn" data-tip="" onclick="showHelp()">? <span id="helpLbl"></span></button>
  <div class="langwrap">
    <button class="iconbtn" id="langBtn" onclick="toggleLang(event)">🌐 <span id="langCur"></span> ▾</button>
    <div class="menu" id="langMenu">
      <div data-l="en" onclick="setLang('en')">English</div>
      <div data-l="zh" onclick="setLang('zh')">简体中文</div>
      <div data-l="ja" onclick="setLang('ja')">日本語</div>
    </div>
  </div>
  <button onclick="openNew()" id="newBtn">+ New</button>
</header>
<div class="layout">
  <div class="sidebar">
    <div class="h" id="sideProjects">Projects</div>
    <div id="projects"></div>
  </div>
  <div class="main" id="main"></div>
</div>

<div class="modal" id="newModal">
  <div class="box">
    <h3 id="mTitle"></h3>
    <div class="muted" id="mDesc" style="font-size:12px;margin-bottom:8px"></div>
    <label id="lDir"></label><input id="f_dir" placeholder="E:\path\to\my-app">
    <label id="lName"></label><input id="f_name">
    <label id="lGoal"></label><textarea id="f_goal"></textarea>
    <div class="row">
      <div><label id="lVer"></label><input id="f_versions" type="number" value="6" min="1"></div>
      <div><label id="lMode"></label><select id="f_mode"><option value="advanced">advanced</option><option value="simple">simple</option></select></div>
    </div>
    <div class="row">
      <div><label id="lProv"></label><select id="f_provider"><option>claude</option><option>codex</option><option>gemini</option></select></div>
      <div><label id="lPcmd"></label><input id="f_pcmd" placeholder="claude"></div>
    </div>
    <label id="lArch"></label><input id="f_hint">
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:11px">
      <input type="checkbox" id="f_brainstorm" style="width:auto;margin:0"> <span id="lBrain"></span>
    </label>
    <div class="hint" id="mTip"></div>
    <div style="margin-top:18px;display:flex;gap:8px;justify-content:flex-end">
      <button class="sec" onclick="closeNew()" id="bCancel"></button>
      <button onclick="createProject()" id="bCreate"></button>
    </div>
  </div>
</div>

<div class="modal" id="bsModal">
  <div class="box">
    <h3 id="bsTitle"></h3>
    <div class="muted" id="bsDesc" style="font-size:12px;margin-bottom:8px"></div>
    <div id="bsLog" class="bs-log"></div>
    <div id="bsFinal"></div>
    <div id="bsInputWrap" class="bs-composer" style="display:none">
      <textarea id="bsReply"></textarea>
      <div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">
        <button class="sec" onclick="bsFinish()" id="bsDone"></button>
        <button onclick="bsSend()" id="bsSendBtn"></button>
      </div>
    </div>
  </div>
</div>

<div class="modal" id="infoModal">
  <div class="box" id="infoBox">
    <h3 id="infoTitle"></h3>
    <div id="infoBody"></div>
    <div style="margin-top:18px;display:flex;justify-content:flex-end">
      <button onclick="closeInfo()" id="infoClose"></button>
    </div>
  </div>
</div>

<div class="tip" id="tip"></div>

<script>
const I18N = {
  en:{brandSub:"autonomous AI iteration · local dashboard",projects:"Projects",noProjects:"No projects yet.",
    selectHint:"Select a project, or create a new one.",newBtn:"+ New project",helpLbl:"Help",close:"Close",deleteProject:"Delete project",
    confirmDelete:"Permanently delete this project and every file in its directory? This cannot be undone.\n\n{path}",deleteRunning:"Stop the active run before deleting this project.",
    tabDash:"Dashboard",tabVer:"Versions",tabDocs:"Docs",tabRepair:"Bug repair",tabSet:"Settings",
    run:"▶ Run",pause:"⏸ Pause agent",resume:"▶ Continue",stopG:"Stop (graceful)",stopI:"Stop (discard version)",
    checkpoint:"Resume checkpoint",nextAgent:"Next agent",lastAgent:"Last completed",humanInput:"Human override",addDirective:"Add directive",
    scopeNext:"Next agent only",scopeVersion:"Rest of this version",scopeFuture:"All future versions",
    repairTitle:"Independent bug repair",repairRequest:"Bug or optimization request",repairStart:"Start repair",repairPromote:"Promote to current",repairTest:"Test command (optional)",
    status:"Status",version:"Version",phase:"Phase",goalProg:"Goal progress",calls:"Agent calls",tokens:"Tokens",runtime:"Run time",
    activeAgents:"Running now",noActive:"No agent running.",liveProgress:"Activity log",noEvents:"No activity yet.",
    outputViewer:"Agent output",outputHint:"Click an output button in the log to view an agent's full output here.",
    cVer:"Version",cPhase:"Phase",cScore:"Score",cTests:"Tests",cGoal:"Goal",cSummary:"Summary",cNew:"What's new",pass:"pass",fail:"fail",
    backlog:"Accepted feature backlog",backlogEmpty:"Empty (fills after the goal is met).",
    featuresTitle:"FEATURES.md (overview table)",changelogTitle:"CHANGELOG.md",
    setTitle:"Run configuration",mode:"Mode",maxVer:"Max versions (default)",provider:"Provider",providerCmd:"Provider command",
    model:"Model (optional)",reviewTh:"Review threshold",valueTh:"Value threshold (feature gate)",fixRetries:"Fix retries",
    maxPar:"Max parallel agents",retries:"Provider call retries",testCmd:"Test command override",gitVer:"Git versioning",
    steps:"Pipeline steps",stepsHint:"Each step is an agent with an editable prompt template. Required agents are always on.",
    grpReq:"Required agents (always on)",grpOpt:"Optional steps",reqd:"required",
    promptTpl:"Prompt templates",promptHint:"Edit the wording in any language. Keep the {{placeholders}} and JSON field names listed below — they are how the engine injects context and reads the reply.",
    reqTokens:"Must keep these tokens:",reqOk:"format OK ✓",reqBad:"missing required tokens",
    save:"Save settings",saved:"saved ✓",locked:"A run is active. Settings are read-only until it stops. Saved settings take effect on the next run.",
    mTitle:"Create a project",mDesc:"This only creates the project. Edit settings first, then press Run on the dashboard.",
    lDir:"Project directory (absolute path)",lName:"Project name",lGoal:"Goal / requirement",lVer:"Max versions",lMode:"Mode",
    lProv:"Provider",lPcmd:"Provider command (optional)",lArch:"Architecture hint (optional)",
    lBrain:"Brainstorm the design first (interactive Q&A)",
    bsTitle:"Brainstorm the design",bsDesc:"The AI asks one question at a time. Answer them to refine the goal before the run.",
    bsAI:"AI",bsYou:"You",bsThinking:"thinking…",bsAgreed:"Design agreed — saved to docs/brainstorm-spec.md.",
    bsSend:"Send",bsDone:"Finish now",bsPlaceholder:"Type your answer…",bsFinishMsg:"Finish now with what we have.",
    bsFinalTitle:"Final design",bsFinalHint:"Review and edit this build brief. The agents will use this exact document when you run the project.",
    designTitle:"Brainstorm design",designEditHint:"Editable until the first run. Save it when you are happy; this document becomes the agents' build brief.",
    designLockedHint:"The project has started, so this agreed design is now read-only.",designSave:"Save design",designSaveRun:"Save & run",designSaved:"Saved ✓",
    brainstormHistoryTitle:"Brainstorm conversation (read-only)",brainstormSpecTitle:"Final brainstorm design",
    mTip:"The provider CLI must already be installed and authenticated locally. No API key is entered here.",
    cancel:"Cancel",create:"Create",on:"on",off:"off",confirmImmediate:"Discard the current unfinished version and roll back to the last completed version?",
    st_running:"running",st_completed:"completed",st_failed:"failed",st_stopped:"stopped",st_initialized:"not started",st_unknown:"unknown",
    stopGT:"Graceful stop requested",stopGB:"The current version will finish, then the run stops — nothing is discarded. Any settings you saved apply to the next run.",
    stopIT:"Stopped immediately",stopIB1:"The unfinished version was discarded and current/ was rolled back to the last completed version (v{n}).",
    stopIB0:"The run was stopped and the unfinished version discarded. There was no earlier completed version to roll back to, so current/ was reset.",
    stopFB:"This server wasn't tracking the process (it may have been restarted), so a graceful stop was requested instead.",
    reqMissTitle:"Prompt format incomplete",reqMissBody:"These templates are missing tokens the engine needs. Add them back, then save again:",
    helpTitle:"AutoDevLoop — Help",
    tip_help:"Open the help guide: what every setting, agent and button does.",tip_lang:"Change the interface language.",
    tip_mode:"simple = a lean loop (plan → develop → test → review) that saves tokens. advanced = adds an architect, goal check, an AI test planner, docs, feature scouting and the value gate.",
    tip_maxVer:"How many versions to build before stopping. The loop keeps going to this number even after the goal is met — it then adds extra useful features.",
    tip_provider:"Which coding-agent CLI to drive: claude, codex or gemini. It must already be installed and logged in on this machine.",
    tip_providerCmd:"Override the command that gets run (a full path or a wrapper). Leave blank to use the provider's default command.",
    tip_model:"Optional model name/alias passed to the CLI. Leave blank to use the CLI's own default model.",
    tip_reviewTh:"If a version's review score (0-100) is below this, a fix pass runs before the version is accepted.",
    tip_valueTh:"In the expand phase, a proposed feature is only accepted when its value score (0-100) reaches this number.",
    tip_fixRetries:"Maximum fix → re-test rounds when tests fail or the score is too low.",
    tip_maxPar:"How many development agents may run at the same time when the planner splits work across files.",
    tip_retries:"Automatic retries when a provider call fails transiently (network / timeout).",
    tip_testCmd:"Force one specific test/build command for every version (e.g. npm test). Leave blank to auto-detect or let the AI choose.",
    tip_gitVer:"When on, each version is committed and tagged inside current/. Falls back to folder snapshots if git is unavailable.",
    tip_run:"Start the loop with the saved settings. Disabled while a run is active.",
    tip_stopG:"Graceful: let the current version finish, then stop. Nothing is discarded.",
    tip_stopI:"Discard: kill the agent now, throw away the unfinished version, and roll current/ back to the last completed version.",
    tip_plan:"Decides what this version delivers and how many dev agents to spawn.",
    tip_dev:"Writes the code for the version (one or more agents working in parallel).",
    tip_test:"Runs the tests / build for the version and reports pass or fail.",
    tip_review:"Scores quality, flags blockers, judges goal completeness and writes the 'what's new' summary.",
    tip_fix:"Repairs the version when tests fail or the score is below the review threshold.",
    tip_arch:"One-time architect: picks the stack, layout and test strategy at the very start of the project.",
    tip_goal_check:"A second, independent agent that confirms whether the original goal is genuinely met.",
    tip_test_agent:"Let an AI choose the test commands (advanced). Off = built-in test detection only, no extra call.",
    tip_doc:"Keeps README / design docs accurate on every version.",
    tip_scout:"After the goal is met, proposes genuinely valuable new features to add next.",
    tip_evaluate:"Independently scores the scouted features; only high-value ones enter the backlog.",
    tip_features_doc:"Writes the FEATURES.md overview table (no AI call).",
    h_modes:"Modes",hb_modes:"simple keeps a lean, cheap loop (plan → develop → test → review). advanced turns on the architect, a separate goal check, an AI test planner, a docs agent, plus feature scouting and the value gate after your goal is reached. You can also toggle individual optional steps below the mode selector.",
    h_pipe:"Pipeline & agents",hb_pipe:"Every version runs a fixed sequence of agents. Required agents always run; optional ones are toggled in Settings. Each agent is driven by an editable prompt template of the same name.",
    h_set:"Settings glossary",hb_set:"Hover the ? next to any field for a one-line explanation. Settings are locked while a run is active and take effect on the next run.",
    h_stop:"Stopping a run",hb_stop:"Graceful stop lets the current version finish and keeps it. Discard stop kills the agent immediately, throws away the half-built version, and rolls the working copy back to the last completed version.",
    h_score:"Scores & goal progress",hb_score:"Score (0-100) is the reviewer's quality rating of a version. Goal progress (0-100%) is how much of your ORIGINAL request is done. Once goal progress reaches 100% / goal is met, the run switches from the build phase to the expand phase and starts proposing extra features.",
    h_sec:"Safety",hb_sec:"AutoDevLoop runs AI-generated code and shell test commands on your machine, unattended. Use a dedicated folder or VM, and keep this dashboard on localhost only — it has no login.",
    h_ph:"Prompt placeholders",hb_ph:"A prompt template is the instruction sent to an agent. Before sending, the engine replaces each {{placeholder}} with real data, and afterwards reads specific JSON fields back. Keep every placeholder its template requires; put it on its own line, introduced by a short label so the model knows what follows. Below: what each one is, which templates use it, and a suggested way to introduce it.",
    colAgent:"Agent",colKind:"Kind",colTmpl:"Prompt",colWhat:"What it does",colPh:"Placeholder",colUsed:"Used in",colEx:"Suggested phrasing",
    tmplInactive:"This template's step is turned off in the current mode/config, so it is not used right now and is read-only. Enable its step (or switch to advanced) to edit it.",
    phHelpLink:"Not sure what a placeholder means? See the Help guide →"},

  zh:{brandSub:"自治 AI 迭代 · 本地面板",projects:"项目",noProjects:"还没有项目。",
    selectHint:"选择一个项目，或新建一个。",newBtn:"+ 新建项目",helpLbl:"帮助",close:"关闭",deleteProject:"删除项目",
    confirmDelete:"确定永久删除这个项目及其目录中的全部文件吗？此操作无法撤销。\n\n{path}",deleteRunning:"请先停止正在运行的任务，再删除项目。",
    tabDash:"总览",tabVer:"版本",tabDocs:"文档",tabRepair:"Bug 修复",tabSet:"设置",
    run:"▶ 运行",pause:"⏸ 暂停当前 Agent",resume:"▶ 继续",stopG:"停止（优雅）",stopI:"停止（废弃本版）",
    checkpoint:"恢复检查点",nextAgent:"下一个 Agent",lastAgent:"最后完成",humanInput:"人工补充（最高优先级）",addDirective:"添加补充",
    scopeNext:"仅下一个 Agent",scopeVersion:"当前版本剩余流程",scopeFuture:"后续所有版本",
    repairTitle:"独立 Bug 修复",repairRequest:"Bug 或优化要求",repairStart:"开始修复",repairPromote:"提升为 current",repairTest:"测试命令（可选）",
    status:"状态",version:"版本",phase:"阶段",goalProg:"目标进度",calls:"Agent 调用",tokens:"Tokens",runtime:"运行时长",
    activeAgents:"正在运行",noActive:"当前没有 agent 在运行。",liveProgress:"活动日志",noEvents:"暂无活动。",
    outputViewer:"Agent 输出",outputHint:"点击日志中的「输出」按钮，可在此查看该 agent 的完整输出。",
    cVer:"版本",cPhase:"阶段",cScore:"评分",cTests:"测试",cGoal:"目标",cSummary:"摘要",cNew:"新增/变化",pass:"通过",fail:"失败",
    backlog:"已接受的功能待办",backlogEmpty:"空（目标达成后才会填充）。",
    featuresTitle:"FEATURES.md（一览表）",changelogTitle:"CHANGELOG.md",
    setTitle:"运行配置",mode:"模式",maxVer:"最大版本数（默认）",provider:"Provider",providerCmd:"Provider 命令",
    model:"模型（可选）",reviewTh:"评审阈值",valueTh:"价值阈值（功能闸门）",fixRetries:"修复重试次数",
    maxPar:"最大并行 agent 数",retries:"Provider 调用重试",testCmd:"测试命令覆盖",gitVer:"Git 版本管理",
    steps:"流程步骤",stepsHint:"每个步骤都是一个 agent，对应一个可编辑的 prompt 模板。必需的 agent 始终开启。",
    grpReq:"必需 agent（始终开启）",grpOpt:"可选步骤",reqd:"必需",
    promptTpl:"Prompt 模板",promptHint:"可用任意语言修改文字，但请保留 {{占位符}} 和下方列出的 JSON 字段名——引擎靠它们注入上下文并解析回复。",
    reqTokens:"必须保留这些标记：",reqOk:"格式正确 ✓",reqBad:"缺少必需标记",
    save:"保存设置",saved:"已保存 ✓",locked:"项目运行中，设置为只读，停止后方可修改。保存的设置在下次运行时生效。",
    mTitle:"新建项目",mDesc:"这里只会创建项目，不会立即运行。先去设置里调整参数，再到总览页点击「运行」。",
    lDir:"项目目录（绝对路径）",lName:"项目名称",lGoal:"目标 / 需求",lVer:"最大版本数",lMode:"模式",
    lProv:"Provider",lPcmd:"Provider 命令（可选）",lArch:"架构提示（可选）",
    lBrain:"先头脑风暴梳理设计（交互式问答）",
    bsTitle:"头脑风暴梳理设计",bsDesc:"AI 每次只问一个问题。逐一回答，在开跑前把目标打磨清楚。",
    bsAI:"AI",bsYou:"你",bsThinking:"思考中…",bsAgreed:"设计已确认——已保存到 docs/brainstorm-spec.md。",
    bsSend:"发送",bsDone:"现在结束",bsPlaceholder:"输入你的回答…",bsFinishMsg:"用现有信息直接结束。",
    bsFinalTitle:"最终方案",bsFinalHint:"请检查并修改这份开发方案。运行项目后，所有 Agent 都会以这份文档作为开发目标。",
    designTitle:"头脑风暴最终方案",designEditHint:"首次运行前可以编辑。满意后保存，运行时 Agent 将直接使用这份文档。",
    designLockedHint:"项目已经运行，最终方案现已锁定为只读。",designSave:"保存方案",designSaveRun:"保存并运行",designSaved:"已保存 ✓",
    brainstormHistoryTitle:"头脑风暴会话履历（只读）",brainstormSpecTitle:"头脑风暴最终方案",
    mTip:"所选 provider 的 CLI 必须已在本地安装并登录。此处不需要填写任何 API key。",
    cancel:"取消",create:"创建",on:"开",off:"关",confirmImmediate:"废弃当前未完成的版本，并回退到上一个已完成的版本？",
    st_running:"运行中",st_completed:"已完成",st_failed:"失败",st_stopped:"已停止",st_initialized:"未开始",st_unknown:"未知",
    stopGT:"已请求优雅停止",stopGB:"当前版本会先跑完，然后停止——不会废弃任何内容。你保存的设置将在下次运行时生效。",
    stopIT:"已立即停止",stopIB1:"未完成的版本已被废弃，current/ 已回退到上一个已完成版本（v{n}）。",
    stopIB0:"运行已停止，未完成的版本已废弃。没有更早的已完成版本可回退，current/ 已被重置。",
    stopFB:"本服务没有在跟踪该进程（可能服务重启过），因此改为请求优雅停止。",
    reqMissTitle:"Prompt 格式不完整",reqMissBody:"以下模板缺少引擎需要的标记。请补回这些标记后再保存：",
    helpTitle:"AutoDevLoop — 帮助",
    tip_help:"打开帮助：解释每个设置、agent 和按钮的作用。",tip_lang:"切换界面语言。",
    tip_mode:"simple = 精简循环（计划 → 开发 → 测试 → 评审），省 token。advanced = 额外加入架构师、目标检查、AI 测试规划、文档、功能发掘和价值闸门。",
    tip_maxVer:"停止前要构建多少个版本。即使目标已达成，循环也会一直跑到这个数——之后会继续添加有用的周边功能。",
    tip_provider:"驱动哪个编码 agent 的 CLI：claude、codex 或 gemini。它必须已在本机安装并登录。",
    tip_providerCmd:"覆盖实际运行的命令（完整路径或包装命令）。留空则使用该 provider 的默认命令。",
    tip_model:"可选，传给 CLI 的模型名/别名。留空则使用 CLI 自己的默认模型。",
    tip_reviewTh:"某版本的评审分（0-100）低于此值时，会先跑一轮修复再接受该版本。",
    tip_valueTh:"扩展阶段，提议的功能只有价值分（0-100）达到此值才会被接受。",
    tip_fixRetries:"测试失败或评分过低时，最多进行多少轮「修复 → 重测」。",
    tip_maxPar:"当计划把工作拆给多个开发 agent 时，最多允许多少个同时运行。",
    tip_retries:"provider 调用因网络/超时等瞬时失败时，自动重试的次数。",
    tip_testCmd:"为每个版本强制使用某条测试/构建命令（如 npm test）。留空则自动检测或由 AI 决定。",
    tip_gitVer:"开启后，每个版本都会在 current/ 内 commit 并打 tag。没有 git 时回退为文件夹快照。",
    tip_run:"用已保存的设置启动循环。运行中时禁用。",
    tip_stopG:"优雅：让当前版本跑完再停，不废弃任何内容。",
    tip_stopI:"废弃：立即终止 agent，丢弃未完成的版本，并把 current/ 回退到上一个已完成版本。",
    tip_plan:"决定本版本要交付什么，以及开几个开发 agent。",
    tip_dev:"编写本版本的代码（一个或多个 agent 并行）。",
    tip_test:"运行本版本的测试/构建，并报告通过或失败。",
    tip_review:"打分、标记阻断问题、判断目标完成度，并写出「本版新增」摘要。",
    tip_fix:"当测试失败或评分低于评审阈值时，修复该版本。",
    tip_arch:"一次性架构师：在项目最开始选定技术栈、目录结构和测试策略。",
    tip_goal_check:"第二个独立 agent，确认原始目标是否真正达成。",
    tip_test_agent:"由 AI 选择测试命令（高级）。关闭则只用内置测试检测，不额外调用。",
    tip_doc:"每个版本维护 README / 设计文档的准确性。",
    tip_scout:"目标达成后，发掘真正有价值的、可继续添加的新功能。",
    tip_evaluate:"独立给发掘到的功能打分；只有高价值的才进入待办清单。",
    tip_features_doc:"生成 FEATURES.md 一览表（不调用 AI）。",
    h_modes:"模式",hb_modes:"simple 保持精简省钱的循环（计划 → 开发 → 测试 → 评审）。advanced 会开启架构师、独立的目标检查、AI 测试规划、文档 agent，以及目标达成后的功能发掘和价值闸门。你也可以在模式选择下方单独开关可选步骤。",
    h_pipe:"流程与 agent",hb_pipe:"每个版本都会按固定顺序运行一串 agent。必需 agent 总会运行；可选的在设置里开关。每个 agent 由同名的、可编辑的 prompt 模板驱动。",
    h_set:"设置词条",hb_set:"把鼠标悬停在字段旁的 ? 上即可看到一行解释。运行中设置为只读，保存的设置在下次运行时生效。",
    h_stop:"停止运行",hb_stop:"优雅停止会让当前版本跑完并保留它。废弃停止会立即终止 agent，丢弃这个还没建完的版本，并把工作目录回退到上一个已完成版本。",
    h_score:"评分与目标进度",hb_score:"评分（0-100）是评审对某版本质量的打分。目标进度（0-100%）是你最初需求完成了多少。一旦目标进度达到 100% / 目标达成，运行会从 build 阶段切到 expand 阶段，并开始提议额外功能。",
    h_sec:"安全",hb_sec:"AutoDevLoop 会在你机器上无人值守地运行 AI 生成的代码和 shell 测试命令。请使用专门的文件夹或虚拟机，并让本面板只绑定 localhost——它没有登录鉴权。",
    h_ph:"Prompt 占位符",hb_ph:"prompt 模板就是发给某个 agent 的指令。发送前，引擎会把每个 {{占位符}} 替换成真实数据；返回后，再从中读取特定的 JSON 字段。请保留该模板要求的所有占位符；把占位符单独放一行，前面加一句简短说明，让模型知道下面是什么内容。下表列出每个占位符的含义、用在哪些模板、以及建议的引导写法。",
    colAgent:"Agent",colKind:"类型",colTmpl:"Prompt",colWhat:"作用",colPh:"占位符",colUsed:"用于模板",colEx:"建议写法",
    tmplInactive:"该模板对应的步骤在当前模式/配置下是关闭的，所以现在用不到，处于只读状态。开启它对应的步骤（或切到 advanced 模式）即可编辑。",
    phHelpLink:"不清楚占位符含义？查看帮助文档 →"},

  ja:{brandSub:"自律型 AI 反復 · ローカルダッシュボード",projects:"プロジェクト",noProjects:"プロジェクトがありません。",
    selectHint:"プロジェクトを選ぶか、新規作成してください。",newBtn:"+ 新規作成",helpLbl:"ヘルプ",close:"閉じる",deleteProject:"プロジェクトを削除",
    confirmDelete:"このプロジェクトとディレクトリ内の全ファイルを完全に削除しますか？元に戻せません。\n\n{path}",deleteRunning:"実行中の処理を停止してからプロジェクトを削除してください。",
    tabDash:"ダッシュボード",tabVer:"バージョン",tabDocs:"ドキュメント",tabSet:"設定",
    run:"▶ 実行",stopG:"停止（安全）",stopI:"停止（破棄）",
    status:"状態",version:"バージョン",phase:"フェーズ",goalProg:"目標達成度",calls:"エージェント呼出",tokens:"トークン",runtime:"実行時間",
    activeAgents:"実行中",noActive:"実行中のエージェントはありません。",liveProgress:"アクティビティログ",noEvents:"まだアクティビティはありません。",
    outputViewer:"エージェント出力",outputHint:"ログ内の「出力」ボタンをクリックすると、ここで全文を表示します。",
    cVer:"バージョン",cPhase:"フェーズ",cScore:"スコア",cTests:"テスト",cGoal:"目標",cSummary:"概要",cNew:"変更点",pass:"成功",fail:"失敗",
    backlog:"承認済み機能バックログ",backlogEmpty:"空（目標達成後に追加されます）。",
    featuresTitle:"FEATURES.md（一覧表）",changelogTitle:"CHANGELOG.md",
    setTitle:"実行設定",mode:"モード",maxVer:"最大バージョン数（既定）",provider:"プロバイダ",providerCmd:"プロバイダコマンド",
    model:"モデル（任意）",reviewTh:"レビュー閾値",valueTh:"価値閾値（機能ゲート）",fixRetries:"修正リトライ回数",
    maxPar:"最大並列エージェント数",retries:"プロバイダ呼出リトライ",testCmd:"テストコマンド上書き",gitVer:"Git バージョン管理",
    steps:"パイプライン手順",stepsHint:"各手順は編集可能なプロンプトテンプレートを持つエージェントです。必須エージェントは常に有効です。",
    grpReq:"必須エージェント（常時オン）",grpOpt:"任意の手順",reqd:"必須",
    promptTpl:"プロンプトテンプレート",promptHint:"文言は任意の言語で編集できますが、{{プレースホルダ}} と下記の JSON フィールド名は残してください。エンジンがそれらでコンテキストを注入し、応答を解析します。",
    reqTokens:"次のトークンは必須です：",reqOk:"形式OK ✓",reqBad:"必須トークンが不足",
    save:"設定を保存",saved:"保存しました ✓",locked:"実行中のため設定は読み取り専用です。停止後に編集できます。保存した設定は次回実行時に反映されます。",
    mTitle:"プロジェクト作成",mDesc:"ここでは作成のみ行い、すぐには実行しません。先に設定を調整し、ダッシュボードで「実行」を押してください。",
    lDir:"プロジェクトディレクトリ（絶対パス）",lName:"プロジェクト名",lGoal:"目標 / 要件",lVer:"最大バージョン数",lMode:"モード",
    lProv:"プロバイダ",lPcmd:"プロバイダコマンド（任意）",lArch:"アーキテクチャのヒント（任意）",
    lBrain:"先に設計をブレインストーミング（対話式Q&A）",
    bsTitle:"設計のブレインストーミング",bsDesc:"AI は1問ずつ質問します。回答して、実行前に目標を磨き込みます。",
    bsAI:"AI",bsYou:"あなた",bsThinking:"考え中…",bsAgreed:"設計が決まりました — docs/brainstorm-spec.md に保存しました。",
    bsSend:"送信",bsDone:"ここで終了",bsPlaceholder:"回答を入力…",bsFinishMsg:"今ある情報で終了してください。",
    bsFinalTitle:"最終設計",bsFinalHint:"この開発仕様を確認・編集してください。実行後、エージェントはこの文書を開発目標として使用します。",
    designTitle:"ブレインストーム最終設計",designEditHint:"初回実行までは編集できます。保存後、この文書がエージェントの開発仕様になります。",
    designLockedHint:"プロジェクトは実行済みのため、最終設計は読み取り専用です。",designSave:"設計を保存",designSaveRun:"保存して実行",designSaved:"保存済み ✓",
    brainstormHistoryTitle:"ブレインストーム会話履歴（読み取り専用）",brainstormSpecTitle:"ブレインストーム最終設計",
    mTip:"選択したプロバイダの CLI は事前にインストール・認証済みである必要があります。API キーの入力は不要です。",
    cancel:"キャンセル",create:"作成",on:"オン",off:"オフ",confirmImmediate:"未完成の現在のバージョンを破棄し、最後に完了したバージョンに戻しますか？",
    st_running:"実行中",st_completed:"完了",st_failed:"失敗",st_stopped:"停止",st_initialized:"未開始",st_unknown:"不明",
    stopGT:"安全な停止を要求しました",stopGB:"現在のバージョンが完了してから停止します。破棄はされません。保存した設定は次回実行時に反映されます。",
    stopIT:"即座に停止しました",stopIB1:"未完成のバージョンは破棄され、current/ は最後に完了したバージョン（v{n}）に戻されました。",
    stopIB0:"実行を停止し、未完成のバージョンを破棄しました。戻せる完了済みバージョンが無かったため current/ はリセットされました。",
    stopFB:"このサーバーはプロセスを追跡していなかった（再起動された可能性）ため、代わりに安全な停止を要求しました。",
    reqMissTitle:"プロンプト形式が不完全です",reqMissBody:"次のテンプレートにエンジンが必要とするトークンがありません。追加してから保存してください：",
    helpTitle:"AutoDevLoop — ヘルプ",
    tip_help:"ヘルプを開く：各設定・エージェント・ボタンの役割を説明します。",tip_lang:"表示言語を変更します。",
    tip_mode:"simple = 軽量ループ（計画 → 開発 → テスト → レビュー）でトークン節約。advanced = アーキテクト、目標チェック、AIテスト計画、ドキュメント、機能探索、価値ゲートを追加。",
    tip_maxVer:"停止までに作るバージョン数。目標達成後もこの数まで続き、その後は有用な追加機能を作ります。",
    tip_provider:"駆動する CLI：claude / codex / gemini。本機にインストール・ログイン済みである必要があります。",
    tip_providerCmd:"実際に実行されるコマンドを上書き（フルパスやラッパー）。空ならプロバイダ既定のコマンド。",
    tip_model:"任意。CLI に渡すモデル名/別名。空なら CLI 既定のモデル。",
    tip_reviewTh:"バージョンのレビュースコア（0-100）がこれ未満なら、採用前に修正を行います。",
    tip_valueTh:"拡張フェーズでは、提案機能の価値スコア（0-100）がこの値に達した場合のみ採用されます。",
    tip_fixRetries:"テスト失敗やスコア不足時の「修正 → 再テスト」の最大回数。",
    tip_maxPar:"計画が作業を分割した際に同時実行できる開発エージェントの数。",
    tip_retries:"ネットワーク/タイムアウト等の一時的失敗時に自動リトライする回数。",
    tip_testCmd:"全バージョンで使うテスト/ビルドコマンドを固定（例: npm test）。空なら自動検出かAIが選択。",
    tip_gitVer:"オンにすると各バージョンを current/ 内で commit してタグ付け。git が無ければフォルダスナップショットに退避。",
    tip_run:"保存済み設定でループを開始。実行中は無効。",
    tip_stopG:"安全：現在のバージョンを完了させてから停止。破棄しません。",
    tip_stopI:"破棄：エージェントを即停止し、未完成バージョンを捨て、current/ を最後の完了版に戻します。",
    tip_plan:"このバージョンで何を提供するか、開発エージェントを何体にするかを決定。",
    tip_dev:"バージョンのコードを記述（複数体が並列で動作）。",
    tip_test:"バージョンのテスト/ビルドを実行し、成功か失敗かを報告。",
    tip_review:"品質を採点し、阻害要因を指摘、目標達成度を判定、変更点の要約を作成。",
    tip_fix:"テスト失敗やスコアが閾値未満のとき、バージョンを修復。",
    tip_arch:"一度きりのアーキテクト：開始時にスタック・構成・テスト戦略を決定。",
    tip_goal_check:"元の目標が本当に達成されたかを確認する独立エージェント。",
    tip_test_agent:"AI にテストコマンドを選ばせる（advanced）。オフなら組み込み検出のみ。",
    tip_doc:"各バージョンで README / 設計文書を正確に保つ。",
    tip_scout:"目標達成後、本当に価値ある新機能を提案。",
    tip_evaluate:"探索した機能を独立して採点。高価値のものだけバックログ入り。",
    tip_features_doc:"FEATURES.md 一覧表を作成（AI呼出なし）。",
    h_modes:"モード",hb_modes:"simple は軽量・低コストなループ（計画 → 開発 → テスト → レビュー）。advanced はアーキテクト、独立した目標チェック、AIテスト計画、ドキュメント、目標達成後の機能探索と価値ゲートを有効化します。モード選択の下で各任意手順を個別に切替もできます。",
    h_pipe:"パイプラインとエージェント",hb_pipe:"各バージョンは固定順のエージェント列を実行します。必須は常に実行、任意は設定で切替。各エージェントは同名の編集可能なプロンプトで駆動します。",
    h_set:"設定用語",hb_set:"各項目の ? にカーソルを合わせると一行説明が出ます。実行中は読み取り専用、保存内容は次回実行時に反映。",
    h_stop:"実行の停止",hb_stop:"安全停止は現在のバージョンを完了させて保持します。破棄停止はエージェントを即停止し、作りかけのバージョンを捨て、作業コピーを最後の完了版に戻します。",
    h_score:"スコアと目標達成度",hb_score:"スコア（0-100）はレビューによる品質評価。目標達成度（0-100%）は元の要件の達成割合。100%/目標達成になると build フェーズから expand フェーズへ移り、追加機能の提案を始めます。",
    h_sec:"安全性",hb_sec:"AutoDevLoop は AI 生成コードと shell テストを無人で実行します。専用フォルダや VM を使い、本ダッシュボードは localhost のみに——ログイン認証はありません。",
    h_ph:"プロンプトのプレースホルダ",hb_ph:"プロンプトテンプレートはエージェントへ送る指示です。送信前にエンジンが各 {{プレースホルダ}} を実データに置換し、返答から特定の JSON フィールドを読み取ります。テンプレートが要求するプレースホルダは必ず残し、独立した行に置き、短いラベルで何が続くか示してください。下表に各プレースホルダの意味・使用テンプレート・推奨の導入文を示します。",
    colAgent:"エージェント",colKind:"種別",colTmpl:"プロンプト",colWhat:"役割",colPh:"プレースホルダ",colUsed:"使用テンプレート",colEx:"推奨の書き方",
    tmplInactive:"このテンプレートの手順は現在のモード/設定でオフのため、今は使われず読み取り専用です。対応手順を有効化（または advanced に切替）すると編集できます。",
    phHelpLink:"プレースホルダの意味が不明ですか？ヘルプを参照 →"}
};
const LANG_LABEL={en:"EN",zh:"中文",ja:"日本語"};
const REQUIRED_STEPS=[{key:"plan",agent:"AgentPLAN",tmpl:"plan"},{key:"dev",agent:"AgentDEV",tmpl:"dev"},
  {key:"test",agent:"AgentTEST",tmpl:"test"},{key:"review",agent:"AgentREVIEW",tmpl:"review"},{key:"fix",agent:"AgentFIX",tmpl:"fix"}];
const OPTIONAL_STEPS=[{key:"arch",agent:"AgentARCH",tmpl:"arch"},{key:"goal_check",agent:"AgentGOALCHECK",tmpl:"goal_check"},
  {key:"test_agent",agent:"AgentTEST",tmpl:"test"},{key:"doc",agent:"AgentDOC",tmpl:"doc"},{key:"scout",agent:"AgentSCOUT",tmpl:"scout"},
  {key:"evaluate",agent:"AgentEVALUATE",tmpl:"evaluate"},{key:"features_doc",agent:"—",tmpl:"—"}];
// Mirror config.py: which optional steps each mode runs by default.
const SIMPLE_STEPS={arch:true,goal_check:false,test_agent:false,doc:false,scout:false,evaluate:false,features_doc:true};
const ADVANCED_STEPS={arch:true,goal_check:true,test_agent:true,doc:true,scout:true,evaluate:true,features_doc:true};
// Which optional step gates each prompt template (null = always used / required).
const TMPL_STEP={arch:"arch",plan:null,dev:null,doc:"doc",test:"test_agent",review:null,fix:null,scout:"scout",evaluate:"evaluate",goal_check:"goal_check"};
const PROVIDER_AGENTS=["brainstorm","arch","plan","dev","doc","test","review","fix","goal_check","scout","evaluate","bugfix","bugverify"];
// Per-placeholder docs for the Help guide: where used + meaning + a suggested
// lead-in phrase, in each language. [desc, example phrasing].
const PH={
  goal:{used:"all",en:["The user's overall goal/requirement for the whole project; present in almost every template.","User goal:"],zh:["用户对整个项目的总目标/需求，几乎每个模板都会用到。","用户目标："],ja:["プロジェクト全体の目標/要件。ほぼ全テンプレートで使用。","ユーザー目標："]},
  arch_hint:{used:"arch",en:["Optional architecture hint the user typed when creating the project (may be empty). Only AgentARCH sees it.","Extra architecture hints from the user (may be empty):"],zh:["创建项目时用户填写的可选架构提示（可能为空）。仅 AgentARCH 用到。","用户提供的额外架构提示（可能为空）："],ja:["作成時に入力した任意のアーキテクチャヒント（空の場合あり）。AgentARCH のみ使用。","ユーザーからの追加アーキテクチャヒント（空の場合あり）："]},
  version:{used:"plan, dev, doc, test, review, fix, scout",en:["The version number currently being built (1, 2, 3…).","for version v{{version}}"],zh:["当前正在构建的版本号（1、2、3……）。","针对版本 v{{version}}"],ja:["現在構築中のバージョン番号（1, 2, 3…）。","バージョン v{{version}} 向け"]},
  phase:{used:"plan, review",en:["Which phase the loop is in: build (driving at the goal) or expand (adding features after it is met).","Phase: {{phase}}"],zh:["循环所处阶段：build（冲目标）或 expand（达成后加功能）。","当前阶段：{{phase}}"],ja:["ループの段階：build（目標へ）か expand（達成後の機能追加）。","フェーズ：{{phase}}"]},
  architecture:{used:"plan, dev",en:["The architecture report AgentARCH produced (stack, layout, test strategy). Stay consistent with it.","Architecture contract (stay consistent with this):"],zh:["AgentARCH 产出的架构报告（技术栈、目录、测试策略）。请与之保持一致。","架构约定（请与此保持一致）："],ja:["AgentARCH 作成のアーキテクチャ報告（スタック・構成・テスト戦略）。これに従う。","アーキテクチャ規約（これに従う）："]},
  phase_guidance:{used:"plan",en:["Auto-generated guidance telling the planner whether to complete the goal (build) or extend it (expand).","Guidance for this version:"],zh:["自动生成的指引，告诉计划 agent 是冲目标（build）还是扩展（expand）。","本版本指引："],ja:["計画に build か expand かを伝える自動生成の指針。","本バージョンの指針："]},
  backlog:{used:"plan, scout",en:["The list of accepted future features (filled during the expand phase). Pick from here when extending.","Accepted feature backlog:"],zh:["已接受的待开发功能清单（扩展阶段填充）。扩展时从这里挑。","已接受的功能待办："],ja:["承認済みの今後の機能リスト（拡張フェーズで蓄積）。拡張時はここから選ぶ。","承認済み機能バックログ："]},
  previous:{used:"plan",en:["A summary of the previous iteration: last review, last test result, recent version summaries.","Previous iteration context:"],zh:["上一轮迭代的摘要：上次评审、上次测试结果、最近版本摘要。","上一轮迭代上下文："],ja:["前回反復の要約：前回レビュー・テスト結果・直近バージョン概要。","前回反復のコンテキスト："]},
  context:{used:"plan, test, review, scout, goal_check",en:["A snapshot of the current project's files/contents so the agent can see the real code.","Current project context:"],zh:["当前项目文件/内容的快照，让 agent 能看到真实代码。","当前项目上下文："],ja:["現在のプロジェクトのファイル/内容のスナップショット。実コードを参照可能に。","現在のプロジェクトコンテキスト："]},
  agent_name:{used:"dev",en:["The name of this specific dev agent (the planner may spawn several).","You are {{agent_name}}"],zh:["当前这个开发 agent 的名字（计划可能会开多个）。","你是 {{agent_name}}"],ja:["この開発エージェントの名前（計画が複数生成する場合あり）。","あなたは {{agent_name}} です"]},
  plan:{used:"dev, doc, test, review, fix",en:["The JSON plan for this version (version goal, acceptance criteria, dev agents, test focus).","Version plan:"],zh:["本版本的 JSON 计划（版本目标、验收标准、开发 agent、测试重点）。","版本计划："],ja:["本バージョンの JSON 計画（目標・受入基準・開発エージェント・テスト重点）。","バージョン計画："]},
  task:{used:"dev",en:["This dev agent's specific assignment for the version.","Your specific task:"],zh:["该开发 agent 在本版本中的具体任务。","你的具体任务："],ja:["この開発エージェントの具体的な担当。","あなたの具体的タスク："]},
  owns:{used:"dev",en:["The files/paths this agent should edit, to avoid clobbering peers.","Files you own (edit only these):"],zh:["该 agent 应编辑的文件/路径，避免覆盖其他 agent。","你负责的文件（只改这些）："],ja:["このエージェントが編集すべきファイル/パス（他者の上書き回避）。","担当ファイル（これのみ編集）："]},
  candidates:{used:"test, evaluate",en:["In the test template: detected test/build commands. In evaluate: the scouted feature ideas to score.","Candidates:"],zh:["在 test 模板里：检测到的测试/构建命令；在 evaluate 里：待打分的功能点子。","候选项："],ja:["test では検出したテスト/ビルドコマンド。evaluate では採点対象の機能案。","候補："]},
  test_result:{used:"review, fix",en:["The JSON result of the test run (success flag, commands, output).","Test result:"],zh:["测试运行的 JSON 结果（是否成功、命令、输出）。","测试结果："],ja:["テスト実行の JSON 結果（成否・コマンド・出力）。","テスト結果："]},
  dev_summaries:{used:"review",en:["What each dev agent reported doing this version.","Development agent summaries:"],zh:["本版本各开发 agent 报告自己做了什么。","开发 agent 摘要："],ja:["本バージョンで各開発エージェントが報告した作業内容。","開発エージェント概要："]},
  review:{used:"fix, scout, goal_check",en:["The reviewer's JSON verdict (score, issues, goal progress).","Review:"],zh:["评审的 JSON 结论（评分、问题、目标进度）。","评审结果："],ja:["レビューの JSON 判定（スコア・問題・目標達成度）。","レビュー："]},
  attempt:{used:"fix",en:["Which fix attempt this is (1, 2, …).","fix attempt {{attempt}}"],zh:["这是第几次修复尝试（1、2……）。","第 {{attempt}} 次修复"],ja:["何回目の修正試行か（1, 2…）。","{{attempt}} 回目の修正"]},
  threshold:{used:"evaluate",en:["The minimum value score a feature needs to be accepted by the gate.","A feature is accepted only when value >= {{threshold}}."],zh:["功能被闸门接受所需的最低价值分。","只有价值 >= {{threshold}} 的功能才被接受。"],ja:["機能がゲートに承認されるための最低価値スコア。","価値 >= {{threshold}} の機能のみ承認。"]}
};

let lang = localStorage.getItem('adl_lang') || 'en';
let current=null, tab='dashboard', projects=[], tmplActive=null, cfgCache=null;
let curEvents=[], renderedCount=0, selectedLog=null, lastProgress=null, dashBuilt=false;

function t(k){return (I18N[lang] && I18N[lang][k]) || (I18N.en[k]) || k;}
function h(s){return (s===null||s===undefined)?'':String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function api(p,o){const r=await fetch(p,o);const ct=r.headers.get('content-type')||'';return ct.includes('json')?r.json():r.text();}
function val(id){const e=document.getElementById(id);return e?e.value.trim():'';}
function statusLabel(s){return t('st_'+(s||'unknown'))||s;}
function fmtDur(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;return (h?h+'h ':'')+(h||m?m+'m ':'')+s+'s';}
function helpDot(key){return '<span class="help" data-tip="'+h(t(key))+'">?</span>';}

// ---- floating tooltip ----
const tipEl=document.getElementById('tip');
function posTip(e){const pad=14;let x=e.clientX+pad,y=e.clientY+pad;const w=tipEl.offsetWidth,hh=tipEl.offsetHeight;
  if(x+w>window.innerWidth-8)x=window.innerWidth-w-8;if(y+hh>window.innerHeight-8)y=e.clientY-hh-pad;
  tipEl.style.left=Math.max(8,x)+'px';tipEl.style.top=Math.max(8,y)+'px';}
document.addEventListener('mouseover',e=>{const el=e.target.closest('[data-tip]');if(el&&el.getAttribute('data-tip')){tipEl.textContent=el.getAttribute('data-tip');tipEl.style.display='block';posTip(e);}});
document.addEventListener('mousemove',e=>{if(tipEl.style.display==='block'){const el=e.target.closest('[data-tip]');if(el)posTip(e);else tipEl.style.display='none';}});
document.addEventListener('mouseout',e=>{const el=e.target.closest('[data-tip]');if(el)tipEl.style.display='none';});

// ---- language menu ----
function toggleLang(e){e.stopPropagation();document.getElementById('langMenu').classList.toggle('show');}
document.addEventListener('click',()=>document.getElementById('langMenu').classList.remove('show'));
function setLang(l){lang=l;localStorage.setItem('adl_lang',l);document.getElementById('langMenu').classList.remove('show');applyStatic();dashBuilt=false;render();loadProjects();}
function applyStatic(){
  document.getElementById('brandSub').textContent=t('brandSub');
  document.getElementById('sideProjects').textContent=t('projects');
  document.getElementById('newBtn').textContent=t('newBtn');
  document.getElementById('helpLbl').textContent=t('helpLbl');
  document.getElementById('helpBtn').setAttribute('data-tip',t('tip_help'));
  document.getElementById('langBtn').setAttribute('data-tip',t('tip_lang'));
  document.getElementById('langCur').textContent=LANG_LABEL[lang];
  document.querySelectorAll('#langMenu div').forEach(d=>d.classList.toggle('sel',d.getAttribute('data-l')===lang));
}

// ---- generic info / help modal ----
function showInfo(title,bodyHtml,wide){
  document.getElementById('infoTitle').textContent=title;
  document.getElementById('infoBody').innerHTML=bodyHtml;
  document.getElementById('infoClose').textContent=t('close');
  document.getElementById('infoBox').classList.toggle('wide',!!wide);
  document.getElementById('infoModal').classList.add('show');
}
function closeInfo(){document.getElementById('infoModal').classList.remove('show');}
function showHelp(){
  const secs=[['h_modes','hb_modes'],['h_pipe','hb_pipe'],['h_set','hb_set'],['h_stop','hb_stop'],['h_score','hb_score'],['h_sec','hb_sec']];
  let html='';
  html+='<div class="helpsec"><h4>'+h(t('h_modes'))+'</h4><p>'+h(t('hb_modes'))+'</p></div>';
  // pipeline section + auto table
  let rows='';
  REQUIRED_STEPS.forEach(s=>{rows+=pipeRow(s,true);});
  OPTIONAL_STEPS.forEach(s=>{rows+=pipeRow(s,false);});
  html+='<div class="helpsec"><h4>'+h(t('h_pipe'))+'</h4><p>'+h(t('hb_pipe'))+'</p>'
    +'<table><thead><tr><th>'+h(t('colAgent'))+'</th><th>'+h(t('colKind'))+'</th><th>'+h(t('colTmpl'))+'</th><th>'+h(t('colWhat'))+'</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  // prompt placeholders section
  html+='<div class="helpsec" id="help_ph"><h4>'+h(t('h_ph'))+'</h4><p>'+h(t('hb_ph'))+'</p>'
    +'<table><thead><tr><th>'+h(t('colPh'))+'</th><th>'+h(t('colUsed'))+'</th><th>'+h(t('colWhat'))+'</th><th>'+h(t('colEx'))+'</th></tr></thead><tbody>'+phRows()+'</tbody></table></div>';
  secs.slice(2).forEach(s=>{html+='<div class="helpsec"><h4>'+h(t(s[0]))+'</h4><p>'+h(t(s[1]))+'</p></div>';});
  showInfo(t('helpTitle'),html,true);
}
function phRows(){
  return Object.keys(PH).map(k=>{const e=PH[k][lang]||PH[k].en;
    return '<tr><td><code>{{'+h(k)+'}}</code></td><td class="muted">'+h(PH[k].used)+'</td><td>'+h(e[0])+'</td><td><code>'+h(e[1])+'</code></td></tr>';
  }).join('');
}
function pipeRow(s,req){
  const kind=req?('<span class="badge">'+h(t('reqd'))+'</span>'):('<span class="badge warn">'+h(t('grpOpt'))+'</span>');
  return '<tr><td><b>'+h(s.agent)+'</b></td><td>'+kind+'</td><td><code>'+h(s.tmpl)+'</code></td><td>'+h(t('tip_'+s.key))+'</td></tr>';
}

function openNew(){
  const m=document.getElementById('newModal');m.classList.add('show');
  document.getElementById('mTitle').textContent=t('mTitle');
  document.getElementById('mDesc').textContent=t('mDesc');
  document.getElementById('lDir').textContent=t('lDir');document.getElementById('lName').textContent=t('lName');
  document.getElementById('lGoal').textContent=t('lGoal');document.getElementById('lVer').textContent=t('lVer');
  document.getElementById('lMode').textContent=t('lMode');document.getElementById('lProv').textContent=t('lProv');
  document.getElementById('lPcmd').textContent=t('lPcmd');document.getElementById('lArch').textContent=t('lArch');
  document.getElementById('lBrain').textContent=t('lBrain');
  document.getElementById('mTip').textContent=t('mTip');
  document.getElementById('bCancel').textContent=t('cancel');document.getElementById('bCreate').textContent=t('create');
}
function closeNew(){document.getElementById('newModal').classList.remove('show');}
async function createProject(){
  const brainstorm=document.getElementById('f_brainstorm').checked;
  const payload={dir:val('f_dir'),name:val('f_name'),goal:val('f_goal'),max_versions:val('f_versions'),
    mode:val('f_mode'),provider:val('f_provider'),provider_command:val('f_pcmd'),arch_hint:val('f_hint'),brainstorm};
  const res=await api('/api/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(res.ok){closeNew();await loadProjects();selectDir(res.dir);
    if(res.brainstorm){bsOpen(res.dir);} else {tab='settings';dashBuilt=false;render();}}
  else alert(res.error||'failed');
}

// ---- Brainstorming chat (1 request == 1 LLM turn; state lives server-side) ----
let bsDir=null;
function bsOpen(dir){
  bsDir=dir;
  document.getElementById('bsModal').classList.add('show');
  document.getElementById('bsTitle').textContent=t('bsTitle');
  document.getElementById('bsDesc').textContent=t('bsDesc');
  document.getElementById('bsDone').textContent=t('bsDone');
  document.getElementById('bsSendBtn').textContent=t('bsSend');
  document.getElementById('bsReply').placeholder=t('bsPlaceholder');
  document.getElementById('bsLog').innerHTML='';
  document.getElementById('bsFinal').innerHTML='';
  document.getElementById('bsInputWrap').style.display='none';
  bsTurn('');
}
function bsClose(){document.getElementById('bsModal').classList.remove('show');tab='dashboard';dashBuilt=false;render();}
function bsAppend(role,text){
  const log=document.getElementById('bsLog');
  const user=role==='you',who=user?t('bsYou'):t('bsAI'),avatar=user?h(who).slice(0,1):'✦';
  log.insertAdjacentHTML('beforeend','<div class="bs-msg '+(user?'user':'ai')+'">'
    +'<div class="bs-avatar">'+avatar+'</div><div class="bs-content"><div class="bs-who">'+h(who)+'</div>'
    +'<div class="bs-bubble">'+h(text)+'</div></div></div>');
  log.scrollTop=log.scrollHeight;
}
function bsShowThinking(){
  const log=document.getElementById('bsLog');
  log.insertAdjacentHTML('beforeend','<div class="bs-msg ai bs-thinking" id="bsThinking"><div class="bs-avatar">✦</div>'
    +'<div class="bs-content"><div class="bs-who">'+h(t('bsAI'))+'</div><div class="bs-bubble"><span class="bs-spark"></span>'
    +'<span>'+h(t('bsThinking'))+'</span><span class="bs-dots"><i></i><i></i><i></i></span></div></div></div>');
  log.scrollTop=log.scrollHeight;
}
function bsClearChoices(){const el=document.getElementById('bsChoices');if(el)el.remove();}
function bsShowFinal(spec){
  document.getElementById('bsInputWrap').style.display='none';bsClearChoices();
  document.getElementById('bsFinal').innerHTML='<div class="bs-final"><h4>'+h(t('bsFinalTitle'))+'</h4>'
    +'<div class="hint">'+h(t('bsFinalHint'))+'</div><textarea id="bsFinalEditor">'+h(spec||'')+'</textarea>'
    +'<div class="bs-final-actions"><span class="save-state" id="bsFinalState"></span>'
    +'<button class="sec" onclick="bsClose()">'+h(t('close'))+'</button>'
    +'<button onclick="bsSaveFinal(false)">'+h(t('designSave'))+'</button>'
    +'<button class="ok" onclick="bsSaveFinal(true)">▶ '+h(t('designSaveRun'))+'</button></div></div>';
}
async function bsSaveFinal(runAfter){
  const ed=document.getElementById('bsFinalEditor');if(!ed)return false;
  const res=await api('/api/brainstorm/design',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:bsDir,spec:ed.value})});
  if(!res.ok){alert(res.error||'failed');return false;}
  const st=document.getElementById('bsFinalState');if(st)st.textContent=t('designSaved');
  if(runAfter){document.getElementById('bsModal').classList.remove('show');current=bsDir;await doRun(true);}
  return true;
}
async function bsTurn(reply){
  document.getElementById('bsInputWrap').style.display='none';
  bsShowThinking();
  let res;
  try{res=await api('/api/brainstorm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:bsDir,reply})});}
  catch(err){res={ok:false,error:String(err)};}
  const thinking=document.getElementById('bsThinking');if(thinking)thinking.remove();
  if(!res.ok){bsAppend('ai','⚠ '+(res.error||'failed'));return;}
  if(res.done){
    bsAppend('ai',t('bsAgreed'));
    bsShowFinal(res.spec||res.refined_goal||'');loadProjects();return;
  }
  bsAppend('ai',res.question||'');
  if(res.choices&&res.choices.length){
    const log=document.getElementById('bsLog');
    log.insertAdjacentHTML('beforeend','<div class="bs-choices" id="bsChoices">'
      +res.choices.map(c=>'<button onclick="bsPick(this)">'+h(c)+'</button>').join('')+'</div>');
    log.scrollTop=log.scrollHeight;
  }
  document.getElementById('bsReply').value='';
  document.getElementById('bsInputWrap').style.display='block';
  document.getElementById('bsReply').focus();
}
function bsPick(btn){document.getElementById('bsReply').value=btn.textContent;bsSend();}
function bsSend(){
  const r=document.getElementById('bsReply').value.trim();
  if(!r)return;
  bsClearChoices();
  bsAppend('you',r);bsTurn(r);
}
function bsFinish(){const msg=t('bsFinishMsg');bsClearChoices();bsAppend('you',msg);bsTurn(msg);}

async function loadProjects(){
  const d=await api('/api/projects');projects=d.projects||[];
  const box=document.getElementById('projects');
  if(!projects.length){box.innerHTML='<div class="muted">'+t('noProjects')+'</div>';return;}
  box.innerHTML=projects.map((p,i)=>{
    const cls=p.running?'run':(p.status&&p.status.startsWith('completed')?'ok':(p.status==='failed'?'bad':(p.status==='stopped'?'warn':'')));
    return '<div class="proj '+(p.dir===current?'active':'')+'" onclick="selectIdx('+i+')">'
      +'<div class="head"><div class="nm">'+h(p.name)+'</div><button class="del" title="'+h(t('deleteProject'))+'" aria-label="'+h(t('deleteProject'))+'" onclick="deleteProject(event,'+i+')">🗑</button></div><div class="meta">'
      +'<span class="pill '+cls+'">'+(p.running?'<span class="dot"></span>':'')+statusLabel(p.status)+'</span>'
      +'<span>v'+p.current_version+'/'+p.max_versions+'</span><span>'+(p.goal_progress||0)+'%</span></div></div>';
  }).join('');
}
async function deleteProject(event,i){
  event.stopPropagation();
  const p=projects[i];if(!p)return;
  if(p.running){alert(t('deleteRunning'));return;}
  if(!confirm(t('confirmDelete').replace('{path}',p.dir)))return;
  const res=await api('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:p.dir,confirm_dir:p.dir})});
  if(!res.ok){alert(res.error||'failed');return;}
  if(current===p.dir){current=null;tab='dashboard';dashBuilt=false;curEvents=[];renderedCount=0;selectedLog=null;}
  await loadProjects();
  render();
}
function selectIdx(i){selectDir(projects[i].dir);}
function selectDir(dir){current=dir;tab='dashboard';dashBuilt=false;curEvents=[];renderedCount=0;selectedLog=null;loadProjects();render();}
function setTab(tt){tab=tt;dashBuilt=false;render();}
function curProj(){return projects.find(p=>p.dir===current);}

async function render(){
  const main=document.getElementById('main');
  if(!current){main.innerHTML='<div class="muted">'+t('selectHint')+'</div>';return;}
  const tabs='<div class="tabs">'
    +['dashboard','versions','docs','repair','settings'].map(x=>'<div class="t '+(tab===x?'active':'')+'" onclick="setTab(\''+x+'\')">'+t(x==='dashboard'?'tabDash':x==='versions'?'tabVer':x==='docs'?'tabDocs':x==='repair'?'tabRepair':'tabSet')+'</div>').join('')
    +'</div>';
  if(tab==='dashboard'){ if(!dashBuilt){ main.innerHTML=tabs+dashSkeleton(); dashBuilt=true; curEvents=[];renderedCount=0; } await renderBrainstormDesign(); await pollDashboard(true); }
  else if(tab==='versions'){ main.innerHTML=tabs+await renderVersions(); }
  else if(tab==='docs'){ main.innerHTML=tabs+await renderDocs(); }
  else if(tab==='repair'){ main.innerHTML=tabs+await renderRepairs(); }
  else if(tab==='settings'){ main.innerHTML=tabs+await renderSettings(); bindTemplate(); }
}

function dashSkeleton(){
  return '<div class="toolbar">'
    +'<button class="ok" id="btnRun" data-tip="'+h(t('tip_run'))+'" onclick="doRun()">'+t('run')+'</button>'
    +'<button class="ok" id="btnResume" onclick="doResume()">'+t('resume')+'</button>'
    +'<button class="sec" id="btnPause" onclick="doPause()">'+t('pause')+'</button>'
    +'<button class="warn" id="btnStopG" data-tip="'+h(t('tip_stopG'))+'" onclick="doStop(\'graceful\')">'+t('stopG')+'</button>'
    +'<button class="danger" id="btnStopI" data-tip="'+h(t('tip_stopI'))+'" onclick="doStop(\'immediate\')">'+t('stopI')+'</button>'
    +'<span class="rt">'+t('runtime')+': <b id="rtVal">-</b></span></div>'
    +'<div id="brainstormDesign"></div><div id="checkpointBox"></div>'
    +'<div class="cards" id="cards"></div>'
    +'<div class="panel"><h3>'+t('activeAgents')+'</h3><div class="agents" id="agents"></div></div>'
    +'<div class="panel"><h3>'+t('liveProgress')+'</h3><div class="events" id="events"></div></div>'
    +'<div class="panel"><div class="viewer-head"><h3 style="margin:0">'+t('outputViewer')+'</h3><span class="muted" id="viewerName"></span></div>'
    +'<pre id="logview" class="muted">'+t('outputHint')+'</pre></div>';
}

async function renderBrainstormDesign(){
  const box=document.getElementById('brainstormDesign');if(!box||!current)return;
  const d=await api('/api/brainstorm/design?dir='+encodeURIComponent(current));
  if(!d.exists){box.innerHTML='';return;}
  const hint=d.editable?t('designEditHint'):t('designLockedHint');
  const body=d.editable
    ?'<textarea id="designEditor">'+h(d.spec)+'</textarea><div class="design-actions"><span class="save-state" id="designSaveState"></span>'
      +'<button onclick="saveBrainstormDesign(false)">'+h(t('designSave'))+'</button><button class="ok" onclick="saveBrainstormDesign(true)">▶ '+h(t('designSaveRun'))+'</button></div>'
    :'<pre class="design-readonly">'+h(d.spec)+'</pre>';
  box.innerHTML='<div class="panel design-panel"><div class="design-head"><div><h3>✦ '+h(t('designTitle'))+'</h3><div class="hint">'+h(hint)+'</div></div>'
    +(d.editable?'<span class="badge warn">✎</span>':'<span class="badge">🔒</span>')+'</div>'+body+'</div>';
}
async function saveBrainstormDesign(runAfter){
  const ed=document.getElementById('designEditor');if(!ed)return true;
  const res=await api('/api/brainstorm/design',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current,spec:ed.value})});
  if(!res.ok){alert(res.error||'failed');return false;}
  const st=document.getElementById('designSaveState');if(st)st.textContent=t('designSaved');
  if(runAfter)await doRun(true);
  return true;
}

async function pollDashboard(force){
  if(tab!=='dashboard'||!current) return;
  const p=await api('/api/progress?dir='+encodeURIComponent(current));
  lastProgress=p; const running=!!p.running;
  const br=document.getElementById('btnRun'),bc=document.getElementById('btnResume'),bp=document.getElementById('btnPause'),bg=document.getElementById('btnStopG'),bi=document.getElementById('btnStopI');
  const paused=(p.status==='paused');
  if(br){br.disabled=running||paused;bc.disabled=running||!paused;bp.disabled=!running;bg.disabled=!running;bi.disabled=!(running||paused);}
  const cls=running?'run':((p.status||'').startsWith('completed')?'ok':(p.status==='failed'?'bad':(p.status==='stopped'?'warn':'')));
  const gp=p.goal_progress||0,tk=p.tokens||{};
  const cards=document.getElementById('cards');
  if(cards) cards.innerHTML=
    card(t('status'),'<span class="pill '+cls+'">'+(running?'<span class="dot"></span>':'')+statusLabel(p.status)+'</span>','')
    +card(t('version'),'v'+(p.current_version||0)+'/'+(p.max_versions||0),'')
    +card(t('phase'),p.phase==='expand'?'expand':'build',(p.goal_completed_version?('🎯 v'+p.goal_completed_version):''))
    +card(t('goalProg'),gp+'%','<div class="bar"><i style="width:'+gp+'%"></i></div>')
    +card(t('calls'),(p.calls||0),'')
    +card(t('tokens'),(tk.input||0),'↓in · '+(tk.output||0)+' ↑out');
  const agbox=document.getElementById('agents');
  const active=p.active||[];
  if(agbox){
    agbox.innerHTML=active.length?active.map(a=>
      '<div class="agent-row"><span class="nm">'+h(a.agent)+'</span><span class="st">'+h(a.message||'')+'</span>'
      +'<span class="tm" data-start="'+(a.started_ts||0)+'">0s</span></div>').join('')
      :'<div class="muted">'+t('noActive')+'</div>';
  }
  const evs=p.events||[]; const ebox=document.getElementById('events');
  if(ebox){
    if(evs.length<curEvents.length){curEvents=[];renderedCount=0;ebox.innerHTML='';}
    curEvents=evs;
    const atBottom=ebox.scrollHeight-ebox.scrollTop-ebox.clientHeight<40;
    let html='';
    for(let i=renderedCount;i<evs.length;i++){ html+=evHtml(evs[i],i); }
    if(html){ ebox.insertAdjacentHTML('beforeend',html); renderedCount=evs.length; if(atBottom||force) ebox.scrollTop=ebox.scrollHeight; }
    if(!evs.length) ebox.innerHTML='<div class="muted">'+t('noEvents')+'</div>';
  }
  tickTimers();
  await renderCheckpoint(paused);
}
async function renderCheckpoint(paused){
  const box=document.getElementById('checkpointBox');if(!box)return;
  if(document.activeElement&&['directiveText','directiveScope'].includes(document.activeElement.id))return;
  const cp=await api('/api/checkpoint?dir='+encodeURIComponent(current));
  if(!cp||!cp.run_type){box.innerHTML='';return;}
  if(cp.run_type==='repair'){const bg=document.getElementById('btnStopG'),bi=document.getElementById('btnStopI');if(bg)bg.disabled=true;if(bi)bi.disabled=true;}
  let extra='';
  if(paused){
    const d=await api('/api/directives?dir='+encodeURIComponent(current));
    const rows=(d.directives||[]).filter(x=>x.active).map(x=>'<div class="ev"><span class="badge">'+h(x.scope)+'</span><span class="msg">'+h(x.text)+'</span><button class="tiny" onclick="toggleDirective(\''+h(x.id)+'\',false)">×</button></div>').join('');
    extra='<div style="margin-top:12px"><label>'+h(t('humanInput'))+'</label><textarea id="directiveText" style="min-height:76px"></textarea>'
      +'<div class="design-actions"><select id="directiveScope"><option value="version">'+h(t('scopeVersion'))+'</option><option value="next">'+h(t('scopeNext'))+'</option><option value="future">'+h(t('scopeFuture'))+'</option></select>'
      +'<button onclick="addDirective()">'+h(t('addDirective'))+'</button></div>'+rows+'</div>';
  }
  box.innerHTML='<div class="panel"><h3>⏸ '+h(t('checkpoint'))+'</h3><div class="row"><div><span class="muted">v'+h(cp.version)+' · '+h(cp.phase||'')+'</span></div>'
    +'<div><b>'+h(t('lastAgent'))+':</b> '+h(cp.last_completed_agent||'-')+'</div><div><b>'+h(t('nextAgent'))+':</b> '+h(cp.next_agent||'-')+'</div></div>'+extra+'</div>';
}
async function addDirective(){const text=val('directiveText');if(!text)return;await api('/api/directives',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current,text:text,scope:document.getElementById('directiveScope').value})});await renderCheckpoint(true);}
async function toggleDirective(id,active){await api('/api/directives',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current,id:id,active:active})});await renderCheckpoint(true);}
function card(l,v,s){return '<div class="card"><div class="lbl">'+l+'</div><div class="val">'+v+'</div><div class="sub">'+(s||'')+'</div></div>';}
function evHtml(e,i){
  if(e.step==='VERSION_START'){
    const vno=e.vno||e.version||'';
    return '<div class="ev-div">v'+h(vno)+' · '+h(e.phase||'')+'</div>';
  }
  const log = e.log? '<button class="logbtn" onclick="viewLog('+i+')">📄 '+h(e.agent||'output')+'</button>' : '';
  const snip = e.snippet? '<div class="snip">'+h(e.snippet)+'</div>' : '';
  return '<div class="ev"><span class="tm">'+h((e.time||'').slice(11))+'</span>'
    +'<span class="stp">'+h(e.step)+'</span>'+(e.agent?'<span class="ag">'+h(e.agent)+'</span>':'')
    +'<span class="msg">'+h(e.message)+'</span>'+log+snip+'</div>';
}
async function viewLog(i){
  const e=curEvents[i]; if(!e||!e.log) return; selectedLog=e.log;
  const txt=await api('/api/log?dir='+encodeURIComponent(current)+'&name='+encodeURIComponent(e.log));
  const v=document.getElementById('logview'); if(v){v.classList.remove('muted');v.textContent=txt;}
  const vn=document.getElementById('viewerName'); if(vn) vn.textContent=(e.agent||'')+' · '+e.log;
}
function tickTimers(){
  const now=Date.now()/1000;
  document.querySelectorAll('.tm[data-start]').forEach(el=>{const s=parseFloat(el.getAttribute('data-start'))||0;if(s>0)el.textContent=fmtDur(now-s);});
  const rt=document.getElementById('rtVal');
  if(rt&&lastProgress){const st=lastProgress.run_started_ts,en=lastProgress.run_ended_ts;
    if(st){const end=(lastProgress.running||!en)?now:en;rt.textContent=fmtDur(end-st);}else rt.textContent='-';}
}

async function doRun(skipDesignSave){
  if(!skipDesignSave&&document.getElementById('designEditor')){const saved=await saveBrainstormDesign(false);if(!saved)return;}
  const r=await api('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current})});
  if(!r.ok){alert(r.error||'failed');return;}
  await renderBrainstormDesign();setTimeout(()=>pollDashboard(true),600);
}
async function doPause(){const r=await api('/api/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current})});if(!r.ok)alert(r.error||'failed');setTimeout(()=>pollDashboard(true),300);}
async function doResume(){const r=await api('/api/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current})});if(!r.ok)alert(r.error||'failed');setTimeout(()=>pollDashboard(true),500);}
async function doStop(mode){
  if(mode==='immediate'&&!confirm(t('confirmImmediate')))return;
  const r=await api('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current,mode:mode})});
  setTimeout(()=>{pollDashboard(true);loadProjects();},600);
  if(r&&r.ok){
    if(r.mode==='graceful'){ showInfo(t('stopGT'),'<p>'+h(t('stopGB'))+'</p>'); }
    else if(r.mode==='graceful_fallback'){ showInfo(t('stopGT'),'<p>'+h(t('stopFB'))+'</p>'); }
    else if(r.mode==='immediate'){ const body=r.rolled_back?t('stopIB1').replace('{n}',r.rolled_to):t('stopIB0'); showInfo(t('stopIT'),'<p>'+h(body)+'</p>'); }
  } else if(r&&r.error){ alert(r.error); }
}

async function renderVersions(){
  const s=await api('/api/state?dir='+encodeURIComponent(current));
  const gv=s.goal_completed_version;
  const rows=(s.versions||[]).map(v=>{
    const tests=(v.test_result&&v.test_result.success)?'<span class="badge ok">✅ '+t('pass')+'</span>':'<span class="badge bad">❌ '+t('fail')+'</span>';
    const gp=v.goal_progress||0;
    const nw=(v.whats_new||[]).slice(0,3).map(x=>'• '+h(x)).join('<br>');
    return '<tr><td class="nowrap"><b>v'+v.version+'</b>'+(v.version===gv?' 🎯':'')+'</td>'
      +'<td class="nowrap"><span class="badge '+(v.phase==='expand'?'warn':'')+'">'+h(v.phase)+'</span></td>'
      +'<td class="nowrap">'+scoreBadge(v.review_score)+'</td><td class="nowrap">'+tests+'</td>'
      +'<td class="nowrap">'+gp+'%</td><td>'+h(v.feature_summary)+'</td><td class="muted">'+nw+'</td></tr>';
  }).join('');
  const bl=(s.backlog||[]).filter(b=>b.status==='accepted').map(b=>'<tr><td class="nowrap">'+scoreBadge100(b.value)+'</td><td><b>'+h(b.title)+'</b><div class="muted">'+h(b.description)+'</div></td></tr>').join('');
  return '<div class="panel"><h3>'+t('tabVer')+'</h3><table class="vtable"><thead><tr><th>'+t('cVer')+'</th><th>'+t('cPhase')+'</th><th>'+t('cScore')+'</th><th>'+t('cTests')+'</th><th>'+t('cGoal')+'</th><th>'+t('cSummary')+'</th><th>'+t('cNew')+'</th></tr></thead><tbody>'+(rows||'<tr><td colspan=7 class="muted">-</td></tr>')+'</tbody></table></div>'
    +'<div class="panel"><h3>'+t('backlog')+'</h3><table class="vtable"><thead><tr><th>'+t('cScore')+'</th><th>'+t('cSummary')+'</th></tr></thead><tbody>'+(bl||'<tr><td colspan=2 class="muted">'+t('backlogEmpty')+'</td></tr>')+'</tbody></table></div>';
}
function scoreBadge(n){n=parseInt(n)||0;const c=n>=80?'ok':(n>=60?'warn':'bad');return '<span class="badge '+c+'">'+n+'/100</span>';}
function scoreBadge100(n){n=parseInt(n)||0;const c=n>=80?'ok':(n>=60?'warn':'');return '<span class="badge '+c+'">'+n+'</span>';}

async function renderDocs(){
  const d=await api('/api/brainstorm/design?dir='+encodeURIComponent(current));
  const ft=await api('/api/doc?dir='+encodeURIComponent(current)+'&name=features');
  const cl=await api('/api/doc?dir='+encodeURIComponent(current)+'&name=changelog');
  const dp=await api('/api/doc?dir='+encodeURIComponent(current)+'&name=development-progress');
  let html='';
  if(d.exists)html+='<div class="panel"><h3>✦ '+h(t('brainstormSpecTitle'))+'</h3><pre>'+h(d.spec)+'</pre></div>';
  if(d.history)html+='<div class="panel"><h3>'+h(t('brainstormHistoryTitle'))+'</h3><pre>'+h(d.history)+'</pre></div>';
  if(dp&&dp.indexOf('(not available')!==0)html+='<div class="panel"><h3>'+h(t('checkpoint'))+'</h3><pre>'+h(dp)+'</pre></div>';
  return html+'<div class="panel"><h3>'+t('featuresTitle')+'</h3><pre>'+h(ft)+'</pre></div><div class="panel"><h3>'+t('changelogTitle')+'</h3><pre>'+h(cl)+'</pre></div>';
}

async function renderRepairs(){
  const s=await api('/api/state?dir='+encodeURIComponent(current));
  const data=await api('/api/repairs?dir='+encodeURIComponent(current));
  const opts=(s.versions||[]).map(v=>'<option value="'+v.version+'">v'+v.version+' · '+h(v.feature_summary||'')+'</option>').join('');
  const jobs=(data.jobs||[]).map(j=>{
    const ev=(j.events||[]).slice(-12).map(e=>'<div class="ev"><span class="tm">'+h((e.time||'').slice(11))+'</span><span class="stp">'+h(e.step)+'</span><span class="msg">'+h(e.message)+'</span>'+(e.log?'<button class="logbtn" onclick="viewRepairLog(\''+h(e.log)+'\')">📄</button>':'')+'</div>').join('');
    const promote=j.accepted?'<button class="ok" onclick="promoteRepair(\''+h(j.id)+'\')">'+h(t('repairPromote'))+'</button>':'';
    return '<div class="panel"><div class="viewer-head"><h3>'+h(j.id)+' · '+h(j.status)+'</h3><span class="badge '+(j.accepted?'ok':'')+'">'+(j.accepted?'PASS':'attempt '+h(j.attempt||0))+'</span></div><p>'+h(j.request)+'</p><p class="muted">'+h(j.result_dir)+'</p>'+ev+'<div class="design-actions">'+promote+'</div></div>';
  }).join('');
  return '<div class="toolbar"><button class="sec" onclick="doPause()">'+h(t('pause'))+'</button><button class="ok" onclick="doResume()">'+h(t('resume'))+'</button><button class="sec" onclick="render()">↻</button></div>'
    +'<div class="panel"><h3>🛠 '+h(t('repairTitle'))+'</h3><div class="row"><div><label>'+h(t('version'))+'</label><select id="repairVersion">'+opts+'</select></div><div><label>'+h(t('repairTest'))+'</label><input id="repairTest"></div></div><label>'+h(t('repairRequest'))+'</label><textarea id="repairRequest"></textarea><div class="design-actions"><button onclick="startRepair()">'+h(t('repairStart'))+'</button></div></div><div id="repairLog"></div>'+jobs;
}
async function startRepair(){const r=await api('/api/repairs/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current,version:parseInt(val('repairVersion')||'0'),request:val('repairRequest'),test_command:val('repairTest')})});if(!r.ok)alert(r.error||'failed');else setTimeout(()=>render(),500);}
async function promoteRepair(id){if(!confirm('Replace current/ with this repair result? Existing current/ will be backed up.'))return;const r=await api('/api/repairs/promote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:current,job_id:id})});if(!r.ok)alert(r.error||'failed');else alert('Promoted. Backup: '+r.backup);}
async function viewRepairLog(name){const txt=await api('/api/log?dir='+encodeURIComponent(current)+'&name='+encodeURIComponent(name));document.getElementById('repairLog').innerHTML='<div class="panel"><pre>'+h(txt)+'</pre></div>';}

async function renderSettings(){
  cfgCache=await api('/api/config?dir='+encodeURIComponent(current));
  const c=cfgCache.config,names=cfgCache.template_names,locked=cfgCache.running;
  if(!tmplActive||names.indexOf(tmplActive)<0)tmplActive=names[0];
  const dis=locked?'disabled':'';
  const banner=locked?'<div class="banner">'+t('locked')+'</div>':'';
  const tabsHtml=names.map(n=>'<span class="'+(n===tmplActive?'active':'')+'" id="tt_'+n+'" onclick="pickTmpl(\''+n+'\')">'+n+'</span>').join('');
  const lf=(key,inner)=>'<div><label>'+t(key)+helpDot('tip_'+key)+'</label>'+inner+'</div>';
  return banner+'<div class="panel"><h3>'+t('setTitle')+'</h3>'
    +'<div class="row">'
    +lf('mode','<select id="c_mode" onchange="applyMode(this.value)" '+dis+'><option value="advanced" '+(c.pipeline.mode==='advanced'?'selected':'')+'>advanced</option><option value="simple" '+(c.pipeline.mode==='simple'?'selected':'')+'>simple</option></select>')
    +lf('maxVer','<input id="c_versions" type="number" value="'+c.project.max_versions+'" '+dis+'>')+'</div>'
    +'<div class="row">'+lf('provider','<input id="c_provider" value="'+h(c.provider.name)+'" '+dis+'>')
    +lf('providerCmd','<input id="c_pcmd" value="'+h(c.provider.command)+'" placeholder="claude / codex / gemini" '+dis+'>')+'</div>'
    +providerConfigHtml(c,locked)
    +'<div class="row">'+lf('model','<input id="c_model" value="'+h(c.provider.model)+'" '+dis+'>')
    +lf('reviewTh','<input id="c_review" type="number" value="'+c.review.threshold+'" '+dis+'>')+'</div>'
    +'<div class="row">'+lf('valueTh','<input id="c_value" type="number" value="'+c.value.threshold+'" '+dis+'>')
    +lf('fixRetries','<input id="c_fix" type="number" value="'+c.fix.retries+'" '+dis+'>')+'</div>'
    +'<div class="row">'+lf('maxPar','<input id="c_par" type="number" value="'+c.agents.max_parallel+'" '+dis+'>')
    +lf('retries','<input id="c_retries" type="number" value="'+c.agents.retries+'" '+dis+'>')+'</div>'
    +'<div class="row">'+lf('testCmd','<input id="c_test" value="'+h(c.tests.command)+'" '+dis+'>')
    +lf('gitVer','<select id="c_git" '+dis+'><option value="true" '+(c.vcs.git?'selected':'')+'>'+t('on')+'</option><option value="false" '+(!c.vcs.git?'selected':'')+'>'+t('off')+'</option></select>')+'</div>'
    +'<label>'+t('steps')+helpDot('stepsHint')+'</label><div class="hint">'+t('stepsHint')+'</div>'
    +stepsHtml(c,locked)+'</div>'
    +'<div class="panel"><h3>'+t('promptTpl')+'</h3><div class="tmpl-tabs">'+tabsHtml+'</div>'
    +'<textarea id="tmpl_text" oninput="validateActive()" '+dis+'></textarea>'
    +'<div class="tmplnote" id="tmplNote">'+t('tmplInactive')+'</div>'
    +'<div class="hint">'+t('promptHint')+'</div>'
    +'<div style="margin-top:6px"><span class="muted" style="font-size:12px">'+t('reqTokens')+'</span><div class="chips" id="reqChips"></div><div id="reqStatus" class="hint"></div>'
    +'<div style="margin-top:6px"><span class="linklike" onclick="showHelp()">'+t('phHelpLink')+'</span></div></div></div>'
    +(locked?'':'<div style="display:flex;gap:10px;align-items:center"><button id="saveBtn" onclick="saveSettings()">'+t('save')+'</button><span id="savemsg" class="muted"></span></div>');
}
function providerConfigHtml(c,locked){
  const dis=locked?'disabled':'';const profiles=(c.provider&&c.provider.profiles)||{};const assignments=(c.provider&&c.provider.assignments)||{};
  let html='<div class="provider-box"><h4>CLI profiles & Agent routing</h4>';
  ['claude','codex','gemini'].forEach(n=>{const p=profiles[n]||{};html+='<div class="row"><div><label>'+n+' command</label><input id="prof_'+n+'_cmd" value="'+h(p.command||'')+'" placeholder="'+n+'" '+dis+'></div><div><label>'+n+' model</label><input id="prof_'+n+'_model" value="'+h(p.model||'')+'" '+dis+'></div></div>';});
  html+='<div class="steptable"><div class="grouphdr">Agent CLI</div>';
  PROVIDER_AGENTS.forEach(k=>{const cur=assignments[k]||c.provider.name||'claude';html+='<div class="step-row"><span class="sa">Agent'+h(k.toUpperCase())+'</span><select id="assign_'+k+'" '+dis+'>'+['claude','codex','gemini'].map(n=>'<option value="'+n+'" '+(n===cur?'selected':'')+'>'+n+'</option>').join('')+'</select></div>';});
  return html+'</div></div>';
}
function stepsHtml(c,locked){
  const ov=(c.pipeline&&c.pipeline.steps)||{};
  const mode=(c.pipeline&&c.pipeline.mode)||'advanced';
  const base=mode==='simple'?SIMPLE_STEPS:ADVANCED_STEPS;
  const row=(s,req)=>{
    let attrs,off='';
    if(req){ attrs='checked disabled'; }
    else {
      const supported=base[s.key]!==false;          // does this mode run it at all?
      const forceOff=(mode==='simple'&&!supported);  // advanced-only step under simple
      const resolved=(ov[s.key]===true||ov[s.key]===false)?ov[s.key]:base[s.key];
      const checked=forceOff?false:resolved;
      const disabled=locked||forceOff;
      attrs='id="step_'+s.key+'" onchange="refreshTmplState()"'+(checked?' checked':'')+(disabled?' disabled':'');
      if(forceOff)off=' off';
    }
    const lock=req?'<span class="lockicon" data-tip="'+h(t('reqd'))+'">🔒</span>':'';
    return '<div class="step-row'+(req?' req':'')+off+'">'
      +'<input type="checkbox" '+attrs+'>'
      +'<span class="sa">'+h(s.agent)+' '+lock+'</span>'
      +'<span class="stmpl">'+h(s.tmpl)+'</span>'
      +'<span class="sd">'+h(t('tip_'+s.key))+'</span></div>';
  };
  let html='<div class="steptable"><div class="grouphdr">'+t('grpReq')+'</div>';
  REQUIRED_STEPS.forEach(s=>html+=row(s,true));
  html+='<div class="grouphdr">'+t('grpOpt')+'</div>';
  OPTIONAL_STEPS.forEach(s=>html+=row(s,false));
  return html+'</div>';
}
// Live: switching mode resets the optional steps to that mode's defaults and
// disables/greys the advanced-only ones when simple is selected.
function applyMode(mode){
  const base=mode==='simple'?SIMPLE_STEPS:ADVANCED_STEPS;
  const locked=cfgCache&&cfgCache.running;
  OPTIONAL_STEPS.forEach(s=>{
    const el=document.getElementById('step_'+s.key);if(!el)return;
    const forceOff=(mode==='simple'&&base[s.key]===false);
    el.checked=forceOff?false:!!base[s.key];
    el.disabled=!!locked||forceOff;
    const r=el.closest('.step-row');if(r)r.classList.toggle('off',forceOff);
  });
  refreshTmplState();
}
function pickTmpl(n){
  // swap in place (do NOT re-render: that would re-fetch config and drop unsaved edits)
  if(cfgCache){const ta=document.getElementById('tmpl_text');if(ta)cfgCache.templates[tmplActive]=ta.value;}
  tmplActive=n;
  document.querySelectorAll('.tmpl-tabs span').forEach(sp=>sp.classList.toggle('active',sp.id==='tt_'+n));
  bindTemplate();
}
function bindTemplate(){
  if(!cfgCache)return;
  const ta=document.getElementById('tmpl_text');if(ta)ta.value=cfgCache.templates[tmplActive]||'';
  validateActive();
  refreshTmplState();
}
function tmplStepOn(name){const k=TMPL_STEP[name];if(!k)return true;const el=document.getElementById('step_'+k);return el?el.checked:true;}
// Grey the prompt tabs whose step is off, and make the editor read-only for the
// active template when its step is off — so prompts follow the mode like agents.
function refreshTmplState(){
  if(!cfgCache)return;
  (cfgCache.template_names||[]).forEach(n=>{const sp=document.getElementById('tt_'+n);if(sp)sp.classList.toggle('inactive',!tmplStepOn(n));});
  const inactive=!tmplStepOn(tmplActive);
  const ta=document.getElementById('tmpl_text');if(ta)ta.disabled=(!!cfgCache.running)||inactive;
  const note=document.getElementById('tmplNote');if(note)note.style.display=inactive?'block':'none';
}
function missingTokens(name,body){const req=(cfgCache.required_tokens||{})[name]||[];body=body||'';return req.filter(x=>body.indexOf(x)<0);}
function validateActive(){
  if(!cfgCache)return;
  const ta=document.getElementById('tmpl_text');const body=ta?ta.value:(cfgCache.templates[tmplActive]||'');
  const req=(cfgCache.required_tokens||{})[tmplActive]||[];
  const miss=missingTokens(tmplActive,body);
  const chips=document.getElementById('reqChips');
  if(chips)chips.innerHTML=req.map(x=>'<span class="chip'+(miss.indexOf(x)>=0?' miss':'')+'">'+h(x)+'</span>').join('')||'<span class="muted" style="font-size:12px">—</span>';
  const st=document.getElementById('reqStatus');
  if(st){st.textContent=miss.length?(t('reqBad')+': '+miss.join(', ')):t('reqOk');st.style.color=miss.length?'var(--bad)':'var(--ok)';}
  const tabSpan=document.getElementById('tt_'+tmplActive);if(tabSpan)tabSpan.classList.toggle('bad',miss.length>0);
}
async function saveSettings(){
  if(cfgCache){const ta=document.getElementById('tmpl_text');if(ta)cfgCache.templates[tmplActive]=ta.value;}
  // client-side format check across all templates
  const bad={};(cfgCache.template_names||[]).forEach(n=>{const m=missingTokens(n,cfgCache.templates[n]);if(m.length)bad[n]=m;});
  if(Object.keys(bad).length){
    showInfo(t('reqMissTitle'),'<p>'+h(t('reqMissBody'))+'</p>'+Object.keys(bad).map(n=>'<p><b>'+h(n)+'</b>: <code>'+bad[n].map(h).join('</code> <code>')+'</code></p>').join(''));
    return;
  }
  const steps={};['arch','goal_check','test_agent','doc','scout','evaluate','features_doc'].forEach(s=>{const el=document.getElementById('step_'+s);if(el)steps[s]=el.checked;});
  const profiles={};['claude','codex','gemini'].forEach(n=>{profiles[n]={command:val('prof_'+n+'_cmd'),model:val('prof_'+n+'_model'),extra_args:[]};});
  const assignments={};PROVIDER_AGENTS.forEach(k=>{const el=document.getElementById('assign_'+k);assignments[k]=el?el.value:'claude';});
  const config={project:{max_versions:parseInt(val('c_versions')||'6')},
    provider:{name:val('c_provider'),command:val('c_pcmd'),model:val('c_model'),profiles:profiles,assignments:assignments},
    pipeline:{mode:document.getElementById('c_mode').value,steps:steps},
    agents:{max_parallel:parseInt(val('c_par')||'3'),retries:parseInt(val('c_retries')||'3')},
    review:{threshold:parseInt(val('c_review')||'80')},value:{threshold:parseInt(val('c_value')||'65')},
    fix:{retries:parseInt(val('c_fix')||'2')},tests:{command:val('c_test')},vcs:{git:document.getElementById('c_git').value==='true'}};
  const btn=document.getElementById('saveBtn');if(btn)btn.disabled=true;
  const r=await api('/api/config?dir='+encodeURIComponent(current),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:config,templates:cfgCache.templates})});
  const m=document.getElementById('savemsg');
  if(r.ok){if(btn)btn.textContent=t('saved');if(m)m.textContent=t('saved');
    // reload settings from disk so the user sees the page visibly refresh
    setTimeout(()=>{cfgCache=null;render();},700);}
  else{ if(btn)btn.disabled=false;
    if(r.error==='invalid_templates'){showInfo(t('reqMissTitle'),'<p>'+h(t('reqMissBody'))+'</p>'+Object.keys(r.invalid||{}).map(n=>'<p><b>'+h(n)+'</b>: <code>'+(r.invalid[n]||[]).map(h).join('</code> <code>')+'</code></p>').join(''));}
    else if(m)m.textContent=r.error||'error'; }
}

// pollers
setInterval(()=>{ if(current&&tab==='dashboard') pollDashboard(false).catch(()=>{}); loadProjects().catch(()=>{}); },1800);
setInterval(tickTimers,1000);

applyStatic();loadProjects();render();
</script>
</body>
</html>
"""
