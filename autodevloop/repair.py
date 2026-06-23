"""Independent, resumable bug-fix jobs based on immutable version snapshots."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from . import control, llm, prompts, testing
from .config import deep_get, load_config, provider_for_agent
from .util import (
    APP_DIR, collect_context, copy_tree_contents, diff_file_lists, extract_json,
    load_json, now_text, restore_working_dir, safe_rmtree, save_json, slugify,
    write_text,
)


def jobs_dir(root: Path) -> Path:
    return root / APP_DIR / "repairs"


def load_job(root: Path, job_id: str) -> dict[str, Any]:
    data = load_json(jobs_dir(root) / f"{slugify(job_id)}.json", {})
    return data if isinstance(data, dict) else {}


def save_job(root: Path, job: dict[str, Any]) -> None:
    save_json(jobs_dir(root) / f"{job['id']}.json", job)


def list_jobs(root: Path) -> list[dict[str, Any]]:
    base = jobs_dir(root)
    if not base.exists():
        return []
    jobs = [load_json(path, {}) for path in sorted(base.glob("*.json"), reverse=True)]
    return [job for job in jobs if isinstance(job, dict) and job.get("id")]


def create_job(root: Path, version: int, request: str, test_command: str = "") -> dict[str, Any]:
    if not request.strip():
        raise ValueError("bug or optimization request is required")
    source = root / "versions" / f"v{version}"
    if version < 1 or not source.exists():
        raise ValueError(f"completed version v{version} does not exist")
    parent = root / "repairs" / f"v{version}"
    index = 1
    while (parent / f"fix-{index:03d}").exists():
        index += 1
    job_id = f"v{version}-fix-{index:03d}"
    result_dir = parent / f"fix-{index:03d}"
    result_dir.mkdir(parents=True, exist_ok=False)
    copy_tree_contents(source, result_dir)
    job = {
        "id": job_id, "version": version, "request": request.strip(),
        "test_command": test_command.strip(), "status": "initialized",
        "attempt": 0, "max_attempts": 3, "accepted": False,
        "result_dir": str(result_dir), "source_dir": str(source),
        "events": [], "created_at": now_text(),
    }
    save_job(root, job)
    return job


def _event(root: Path, job: dict[str, Any], step: str, message: str, **extra: Any) -> None:
    event = {"time": now_text(), "ts": time.time(), "step": step, "message": message}
    event.update(extra)
    job.setdefault("events", []).append(event)
    job["events"] = job["events"][-200:]
    save_job(root, job)


def _call(root: Path, job: dict[str, Any], key: str, label: str, prompt: str,
          cwd: Path, on_status: Callable[[str], None] | None = None) -> str:
    config = load_config(root)
    profile = provider_for_agent(config, key)
    logs = root / APP_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _event(root, job, key.upper(), "started", agent=label, kind="start", provider=profile.get("name"))
    result = llm.call(
        profile, prompt + control.render_directives(root / APP_DIR, int(job["version"])), cwd,
        label=label, timeout=int(deep_get(config, "agents.timeout", 1800)),
        retries=int(deep_get(config, "agents.retries", 3)),
        backoff_seconds=float(deep_get(config, "agents.backoff_seconds", 5)),
        on_status=on_status,
    )
    log_name = f"repair_{job['id']}_{slugify(label)}.log"
    write_text(logs / log_name, result.text)
    _event(root, job, key.upper(), "completed", agent=label, kind="done", log=log_name,
           snippet=result.text[:500], duration_s=round(result.duration_s, 1))
    return result.text


def run_job(root: Path, job_id: str) -> None:
    root = root.resolve()
    job = load_job(root, job_id)
    if not job:
        raise RuntimeError(f"repair job not found: {job_id}")
    result_dir = Path(job["result_dir"])
    config = load_config(root)
    cp = control.load_checkpoint(root / APP_DIR)
    if cp.get("run_type") == "repair" and cp.get("job_id") == job_id:
        control.restore_active(root / APP_DIR, result_dir)
    else:
        cp = {"run_type": "repair", "job_id": job_id, "version": int(job["version"]),
              "phase": "repair", "status": "running", "completed_steps": [],
              "next_agent": "AgentBUGFIX", "working_dir": str(result_dir)}
        control.snapshot_active(root / APP_DIR, result_dir)
    cp["status"] = "running"
    control.save_checkpoint(root / APP_DIR, cp)
    job["status"] = "running"
    save_job(root, job)

    try:
        feedback = str(job.get("feedback") or "")
        resume_step = str(cp.get("next_agent") or "AgentBUGFIX")
        prior_attempt = int(job.get("attempt", 0) or 0)
        start = prior_attempt if prior_attempt and resume_step in {"Tests", "AgentBUGVERIFY"} else prior_attempt + 1
        for attempt in range(start, int(job.get("max_attempts", 3)) + 1):
            before = root / APP_DIR / "work" / "repairs" / job_id / f"attempt-{attempt}-before"
            workspace = root / APP_DIR / "work" / "repairs" / job_id / f"attempt-{attempt}"
            skip_bugfix = attempt == prior_attempt and resume_step in {"Tests", "AgentBUGVERIFY"}
            if not skip_bugfix:
                safe_rmtree(before, root)
                copy_tree_contents(result_dir, before)
            safe_rmtree(workspace, root)
            if not skip_bugfix:
                copy_tree_contents(result_dir, workspace)
                prompt = prompts.render_template(root / APP_DIR, "bugfix", {
                    "version": job["version"], "request": job["request"], "feedback": feedback or "(none)",
                    "context": collect_context(result_dir),
                })
                _call(root, job, "bugfix", f"AgentBUGFIX_{attempt}", prompt, workspace)
                restore_working_dir(workspace, result_dir)
                job["attempt"] = attempt
                cp.update({"next_agent": "Tests", "last_completed_agent": f"AgentBUGFIX#{attempt}"})
                cp.setdefault("completed_steps", []).append(f"AgentBUGFIX#{attempt}")
                control.snapshot_active(root / APP_DIR, result_dir)
                control.save_checkpoint(root / APP_DIR, cp)
                control.mark_directives_applied(root / APP_DIR, int(job["version"]), f"AgentBUGFIX#{attempt}")

            command = str(job.get("test_command") or deep_get(config, "tests.command", "")).strip()
            candidates = testing.detect_candidates(result_dir)
            if not command:
                command = candidates[0]["command"] if candidates else "__builtin_file_smoke__"
            test_ws = root / APP_DIR / "work" / "repairs" / job_id / f"test-{attempt}"
            safe_rmtree(test_ws, root)
            copy_tree_contents(result_dir, test_ws)
            test_log = root / APP_DIR / "logs" / f"repair_{job_id}_test_{attempt}.log"
            if attempt == prior_attempt and resume_step == "AgentBUGVERIFY":
                test_result = job.get("test_result") or cp.get("test_result") or {"success": False}
            else:
                test_result = testing.run_command(
                    test_ws, command, int(deep_get(config, "tests.timeout", 120)), test_log)
                job["test_result"] = test_result
                _event(root, job, "TEST", "passed" if test_result.get("success") else "failed",
                       agent="Tests", log=test_log.name)
                cp.update({"next_agent": "AgentBUGVERIFY", "last_completed_agent": f"Tests#{attempt}",
                           "test_result": test_result})
                control.save_checkpoint(root / APP_DIR, cp)

            diff = diff_file_lists(before, result_dir)
            verify_ws = root / APP_DIR / "work" / "repairs" / job_id / f"verify-{attempt}"
            safe_rmtree(verify_ws, root)
            copy_tree_contents(result_dir, verify_ws)
            verify_prompt = prompts.render_template(root / APP_DIR, "bugverify", {
                "version": job["version"], "request": job["request"],
                "test_result": json.dumps(test_result, ensure_ascii=False, indent=2),
                "diff": json.dumps(diff, ensure_ascii=False, indent=2),
                "context": collect_context(result_dir, max_bytes=50_000),
            })
            raw = _call(root, job, "bugverify", f"AgentBUGVERIFY_{attempt}", verify_prompt, verify_ws)
            verdict = extract_json(raw, {"accepted": False, "reason": "invalid verification response", "remaining_issues": []})
            accepted = bool(test_result.get("success")) and bool(verdict.get("accepted"))
            job.update({"verification": verdict, "accepted": accepted, "diff": diff})
            cp.update({"last_completed_agent": f"AgentBUGVERIFY#{attempt}",
                       "next_agent": "Complete" if accepted else "AgentBUGFIX"})
            control.save_checkpoint(root / APP_DIR, cp)
            control.mark_directives_applied(root / APP_DIR, int(job["version"]), f"AgentBUGVERIFY#{attempt}")
            save_job(root, job)
            if accepted:
                job["status"] = "completed"
                _event(root, job, "DONE", "repair accepted", kind="done")
                control.clear_checkpoint(root / APP_DIR)
                return
            feedback = str(verdict.get("reason") or "") + "\n" + "\n".join(verdict.get("remaining_issues") or [])
            job["feedback"] = feedback.strip()
            save_job(root, job)
            resume_step = "AgentBUGFIX"
        job["status"] = "failed"
        _event(root, job, "DONE", "maximum repair attempts reached")
        control.clear_checkpoint(root / APP_DIR)
    except Exception as exc:
        control.restore_active(root / APP_DIR, result_dir)
        cp["status"] = "paused"
        cp["pause_reason"] = str(exc)
        control.save_checkpoint(root / APP_DIR, cp)
        job["status"] = "paused"
        job["error"] = str(exc)
        _event(root, job, "ERROR", str(exc), kind="error")
        raise


def promote_job(root: Path, job_id: str) -> dict[str, Any]:
    job = load_job(root, job_id)
    if not job or job.get("status") != "completed" or not job.get("accepted"):
        return {"ok": False, "error": "only an accepted repair can be promoted"}
    current = root / "current"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root / APP_DIR / "promotions" / stamp
    copy_tree_contents(current, backup)
    restore_working_dir(Path(job["result_dir"]), current)
    state_path = root / APP_DIR / "state.json"
    state = load_json(state_path, {})
    state["current_source"] = {"type": "repair", "job_id": job_id, "base_version": job["version"], "promoted_at": now_text()}
    save_json(state_path, state)
    job["promoted_at"] = now_text()
    job["promotion_backup"] = str(backup)
    save_job(root, job)
    return {"ok": True, "backup": str(backup), "current": str(current)}
