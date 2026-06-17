"""The AutoDevLoop orchestration engine."""

from __future__ import annotations

import concurrent.futures
import filecmp
import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import llm, prompts, registry, reporting, testing, vcs
from .config import deep_get, load_config, provider_invocation, resolved_steps
from .util import (
    APP_DIR, INTERNAL_DIRS, DOC_SUFFIXES, PROGRESS_FILE, STATE_FILE, STOP_FILE,
    collect_context, copy_tree_contents, diff_file_lists, extract_json,
    list_generated_files, load_json, now_text, read_text, restore_working_dir,
    safe_rmtree, save_json, slugify, ts, write_text,
)

DEV_DEFAULT = [{"name": "AgentDEV", "role": "general", "task": "Implement the next useful version.", "owns": []}]


def _log(message: str) -> None:
    line = f"[{ts()}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(line.encode(enc, "replace").decode(enc) + "\n")
        sys.stdout.flush()


def _coerce_pct(value: Any, default: int = 0) -> int:
    """Normalise a 0-100 percentage/score.

    Models are inconsistent: some return a 0-1 fraction (e.g. 0.72), some a
    0-100 integer (e.g. 72). We treat any value in (0, 1] as a fraction and
    scale it, then clamp to 0-100. This is why a version could show 0% goal
    progress while a later one showed 95%: ``int(0.72)`` is ``0``.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if 0 < n <= 1:
        n *= 100
    return max(0, min(100, int(round(n))))


def _file_differs(a: Path, b: Path) -> bool:
    if not b.exists():
        return True
    if not a.exists():
        return False
    try:
        return not filecmp.cmp(a, b, shallow=False)
    except OSError:
        return True


class AutoDevLoop:
    def __init__(self, root: Path, config: dict[str, Any], overrides: dict[str, Any] | None = None) -> None:
        self.root = root.resolve()
        self.config = config
        self.overrides = overrides or {}
        self.app_dir = self.root / APP_DIR
        self.state_path = self.app_dir / STATE_FILE
        self.progress_path = self.app_dir / PROGRESS_FILE
        self.stop_path = self.app_dir / STOP_FILE
        self.logs_dir = self.app_dir / "logs"
        self.prompts_dir = self.app_dir / "prompts"
        self.plans_dir = self.app_dir / "plans"
        self.reviews_dir = self.app_dir / "reviews"
        self.tests_dir = self.app_dir / "tests"
        self.work_dir = self.app_dir / "work"
        self.current_dir = self.root / "current"
        self.versions_dir = self.root / "versions"
        self.architecture_path = self.app_dir / "architecture.md"
        self.changelog_path = self.root / "CHANGELOG.md"
        self.features_path = self.root / "FEATURES.md"
        self.report_path = self.app_dir / "final_report.md"
        self.steps = resolved_steps(config)
        self.provider = provider_invocation(config)
        self.cost = {"cost_usd_total": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}
        self._progress: dict[str, Any] = {}
        self._active: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._last_progress_write = 0.0

    # ----- settings helpers ------------------------------------------------
    @property
    def agent_timeout(self) -> int:
        return int(deep_get(self.config, "agents.timeout", 1800))

    @property
    def retries(self) -> int:
        return int(deep_get(self.config, "agents.retries", 3))

    @property
    def backoff(self) -> float:
        return float(deep_get(self.config, "agents.backoff_seconds", 5))

    @property
    def review_threshold(self) -> int:
        return int(deep_get(self.config, "review.threshold", 80))

    @property
    def value_threshold(self) -> int:
        return int(deep_get(self.config, "value.threshold", 65))

    @property
    def fix_retries(self) -> int:
        return int(deep_get(self.config, "fix.retries", 2))

    @property
    def test_timeout(self) -> int:
        return int(deep_get(self.config, "tests.timeout", 120))

    @property
    def allow_parallel(self) -> bool:
        return bool(deep_get(self.config, "agents.allow_parallel", True))

    @property
    def max_parallel(self) -> int:
        return int(deep_get(self.config, "agents.max_parallel", 3))

    @property
    def use_git(self) -> bool:
        return bool(deep_get(self.config, "vcs.git", True))

    # ----- lifecycle -------------------------------------------------------
    def ensure_dirs(self) -> None:
        for path in [self.app_dir, self.logs_dir, self.prompts_dir, self.plans_dir,
                     self.reviews_dir, self.tests_dir, self.work_dir,
                     self.current_dir, self.versions_dir]:
            path.mkdir(parents=True, exist_ok=True)
        prompts.ensure_templates(self.app_dir)

    def run(self, *, reset: bool, goal: str, project_name: str, max_versions: int) -> None:
        if reset:
            for rel in [APP_DIR, "versions", "current"]:
                safe_rmtree(self.root / rel, self.root)

        self.ensure_dirs()
        if self.stop_path.exists():
            self.stop_path.unlink()

        # Preserve event history across resumes / server restarts.
        existing_progress = load_json(self.progress_path, {})
        if isinstance(existing_progress, dict):
            self._progress = existing_progress
        self._progress.setdefault("events", [])

        state = self._load_or_create_state(goal, project_name, max_versions, reset)
        state["status"] = "running"
        state["settings"] = self._settings_snapshot()
        registry.register(self.root, state.get("project_name", ""))
        if self.use_git:
            vcs.ensure_repo(self.current_dir)
        save_json(self.state_path, state)
        with self._lock:
            self._progress["run_started_at"] = now_text()
            self._progress["run_started_ts"] = time.time()
            self._progress["run_ended_at"] = None
            self._progress["run_ended_ts"] = None
        self._emit(state, step="START", agent="", message="run started")

        _log("AutoDevLoop - autonomous AI iteration engine")
        _log(f"Project : {state.get('project_name')} | mode: {deep_get(self.config, 'pipeline.mode')}")
        _log(f"Goal    : {state.get('goal', '')[:100]}")
        _log(f"Versions: {state.get('current_version')} -> {state.get('max_versions')}")
        _log(f"Provider: {self.provider.get('command')} ({self.provider.get('name')})")

        try:
            self._ensure_architecture(state)
            while int(state["current_version"]) < int(state["max_versions"]):
                if self.stop_path.exists():
                    state["status"] = "stopped"
                    state["stop_reason"] = "User requested stop (STOP file)"
                    break
                version = int(state["current_version"]) + 1
                state = self._run_version(version, state)
                save_json(self.state_path, state)

            if state.get("status") == "running":
                state["status"] = "completed"
                state["stop_reason"] = f"Reached max versions ({state.get('max_versions')})"
        except KeyboardInterrupt:
            state = load_json(self.state_path, state)
            state["status"] = "stopped_by_keyboard"
            state["stop_reason"] = "Ctrl+C"
            _log("Stopped by keyboard interrupt.")
        except Exception as exc:  # noqa: BLE001
            state = load_json(self.state_path, state)
            state["status"] = "failed"
            state["last_error"] = str(exc)
            save_json(self.state_path, state)
            self._emit(state, step="ERROR", agent="", message=str(exc)[:200])
            _log(f"Failed: {exc}")
            raise
        finally:
            state["cost"] = self.cost
            save_json(self.state_path, state)
            reporting.write_final_report(self.report_path, state)
            reporting.write_features_overview(self.features_path, state)
            with self._lock:
                self._active.clear()
                self._progress["run_ended_at"] = now_text()
                self._progress["run_ended_ts"] = time.time()
            self._emit(state, step="DONE", agent="", message=state.get("status", ""))
            _log(f"Status: {state.get('status')} | Reason: {state.get('stop_reason', 'N/A')} | "
                 f"Calls: {self.cost['calls']} | Tokens in/out: "
                 f"{self.cost['input_tokens']}/{self.cost['output_tokens']}")

    def _settings_snapshot(self) -> dict[str, Any]:
        return {
            "mode": deep_get(self.config, "pipeline.mode"),
            "steps": self.steps,
            "provider": {k: self.provider.get(k) for k in ("name", "command", "model")},
            "review_threshold": self.review_threshold,
            "value_threshold": self.value_threshold,
            "fix_retries": self.fix_retries,
            "max_versions_default": int(deep_get(self.config, "project.max_versions", 5)),
        }

    def _load_or_create_state(self, goal: str, project_name: str, max_versions: int, reset: bool) -> dict[str, Any]:
        existing = load_json(self.state_path, {})
        if existing and not reset:
            if goal:
                existing["goal"] = goal
            existing["max_versions"] = max_versions or existing.get("max_versions")
            self.cost = existing.get("cost", self.cost)
            existing.setdefault("phase", "build")
            existing.setdefault("backlog", [])
            return existing
        if not goal:
            raise SystemExit("A goal is required (--goal or interactive setup).")
        stamp = now_text()
        return {
            "project_name": project_name or deep_get(self.config, "project.name", "") or self.root.name,
            "goal": goal,
            "arch_hint": deep_get(self.config, "project.arch_hint", ""),
            "current_version": 0,
            "max_versions": max_versions,
            "status": "initialized",
            "phase": "build",
            "goal_met": False,
            "goal_progress": 0,
            "goal_completed_version": None,
            "stop_reason": None,
            "created_at": stamp,
            "updated_at": stamp,
            "architecture_created": False,
            "versions": [],
            "backlog": [],
            "last_review": {},
            "last_test_result": {"success": None, "command": ""},
            "cost": self.cost,
        }

    # ----- progress / events ----------------------------------------------
    def _snapshot_locked(self, state: dict[str, Any]) -> None:
        prog = self._progress
        prog.update({
            "status": state.get("status"),
            "phase": state.get("phase"),
            "project_name": state.get("project_name"),
            "goal": state.get("goal"),
            "current_version": state.get("current_version"),
            "max_versions": state.get("max_versions"),
            "goal_progress": state.get("goal_progress"),
            "goal_met": state.get("goal_met"),
            "goal_completed_version": state.get("goal_completed_version"),
            "calls": self.cost.get("calls", 0),
            "tokens": {"input": self.cost.get("input_tokens", 0), "output": self.cost.get("output_tokens", 0)},
            "active": [dict(a) for a in self._active.values()],
            "updated_at": now_text(),
        })
        prog.setdefault("events", [])
        prog["versions"] = state.get("versions", [])
        save_json(self.progress_path, prog, stamp=False)

    def _snapshot(self, state: dict[str, Any], *, throttle: bool = False) -> None:
        now = time.time()
        with self._lock:
            if throttle and now - self._last_progress_write < 0.6:
                return
            self._last_progress_write = now
            self._snapshot_locked(state)

    def _emit(self, state: dict[str, Any], *, step: str, agent: str, message: str = "",
              extra: dict[str, Any] | None = None) -> None:
        with self._lock:
            prog = self._progress
            prog.setdefault("events", [])
            event = {"time": now_text(), "ts": time.time(), "version": state.get("current_version"),
                     "step": step, "agent": agent, "message": message}
            if extra:
                event.update(extra)
            prog["events"].append(event)
            prog["events"] = prog["events"][-400:]
            self._last_progress_write = time.time()
            self._snapshot_locked(state)

    # ----- LLM helper ------------------------------------------------------
    def _call(self, state: dict[str, Any], label: str, prompt: str, cwd: Path,
              step: str, agent: str) -> str:
        safe = slugify(label.lower())
        version = int(state.get("current_version", 0)) + (0 if step in {"ARCH", "DONE", "START"} else 1)
        prompt_path = self.prompts_dir / f"v{version}_{safe}.prompt.txt"
        write_text(prompt_path, prompt)
        debug_path = self.logs_dir / f"v{version}_{safe}_debug.log"

        with self._lock:
            self._active[agent] = {
                "agent": agent, "step": step, "label": label,
                "started_at": now_text(), "started_ts": time.time(), "message": "calling provider",
            }
        self._emit(state, step=step, agent=agent, message="started", extra={"kind": "start"})
        _log(f"[v{version}] [{label}] calling {self.provider.get('command')} in {cwd.name}...")

        def on_status(msg: str) -> None:
            with self._lock:
                if agent in self._active:
                    self._active[agent]["message"] = msg
            self._snapshot(state, throttle=True)

        try:
            result = llm.call(
                self.provider, prompt, cwd,
                label=label, timeout=self.agent_timeout,
                retries=self.retries, backoff_seconds=self.backoff,
                debug_file=debug_path, on_status=on_status,
            )
        finally:
            with self._lock:
                self._active.pop(agent, None)

        with self._lock:
            self.cost["cost_usd_total"] += result.cost_usd
            self.cost["input_tokens"] += result.input_tokens
            self.cost["output_tokens"] += result.output_tokens
            self.cost["calls"] += 1
        log_name = f"v{version}_{safe}.log"
        write_text(self.logs_dir / log_name, result.text)
        duration = round(result.duration_s, 1)
        self._emit(state, step=step, agent=agent,
                   message=f"done in {duration}s ({result.output_tokens} out tokens)",
                   extra={"kind": "done", "output_tokens": result.output_tokens,
                          "duration_s": duration, "log": log_name, "snippet": result.text.strip()[:500]})
        _log(f"[v{version}] [{label}] done in {duration}s | out {result.output_tokens} tok")
        return result.text

    # ----- pipeline stages -------------------------------------------------
    def _ensure_architecture(self, state: dict[str, Any]) -> None:
        if state.get("architecture_created") and self.architecture_path.exists():
            return
        _log("[ARCH] Designing architecture, stack, and test strategy...")
        prompt = prompts.render_template(self.app_dir, "arch", {
            "goal": state.get("goal", ""),
            "arch_hint": state.get("arch_hint", "") or "(none)",
        })
        output = self._call(state, "ARCH", prompt, self.current_dir, step="ARCH", agent="AgentARCH")
        write_text(self.architecture_path, output.strip() + "\n")
        state["architecture_created"] = True
        if self.use_git:
            vcs.commit_all(self.current_dir, "chore: initial architecture")
        save_json(self.state_path, state)

    def _run_version(self, version: int, state: dict[str, Any]) -> dict[str, Any]:
        _log("=" * 60)
        _log(f"[v{version}] Starting (phase: {state.get('phase')}) of {state.get('max_versions')}")
        self._emit(state, step="VERSION_START", agent="",
                   message=f"v{version} · {state.get('phase')} phase",
                   extra={"kind": "version_start", "vno": version, "phase": state.get("phase")})
        before_dir = self.work_dir / f"v{version}" / "_before"
        safe_rmtree(before_dir, self.root)
        copy_tree_contents(self.current_dir, before_dir)

        try:
            plan = self._plan(version, state)
            dev_outputs = self._develop(version, state, plan, before_dir)
            if self.steps.get("doc"):
                dev_outputs.append(self._doc(version, state, plan))
            test_result = self._test(version, state, plan)
            review = self._review(version, state, plan, test_result, dev_outputs)

            if self._needs_fix(test_result, review):
                for attempt in range(1, self.fix_retries + 1):
                    _log(f"[v{version}] Fix attempt {attempt}/{self.fix_retries}")
                    self._fix(version, state, plan, test_result, review, attempt)
                    test_result = self._test(version, state, plan, suffix=f"fix{attempt}")
                    review = self._review(version, state, plan, test_result, dev_outputs, suffix=f"fix{attempt}")
                    if not self._needs_fix(test_result, review):
                        break

            goal_met, goal_progress = self._assess_goal(version, state, review)
            review["goal_met"] = goal_met
            review["goal_progress"] = goal_progress
        except Exception:
            # Roll back the working copy so a resume re-runs this version cleanly.
            # restore_working_dir keeps current/.git intact.
            _log(f"[v{version}] Error during version; rolling back current/ from snapshot.")
            restore_working_dir(before_dir, self.current_dir)
            raise

        state["goal_met"] = goal_met
        state["goal_progress"] = goal_progress
        newly_completed = goal_met and state.get("phase") == "build"
        if newly_completed:
            state["phase"] = "expand"
            state["goal_completed_version"] = version
            _log(f"[v{version}] 🎯 Core goal met. Switching to EXPAND phase.")

        if state.get("phase") == "expand" and self.steps.get("scout"):
            self._scout_and_evaluate(version, state, review)

        # snapshot + vcs
        diff = diff_file_lists(before_dir, self.current_dir)
        version_dir = self.versions_dir / f"v{version}"
        safe_rmtree(version_dir, self.root)
        version_dir.mkdir(parents=True, exist_ok=True)
        copy_tree_contents(self.current_dir, version_dir)
        commit = None
        if self.use_git:
            commit = vcs.commit_all(self.current_dir, f"v{version}: {plan.get('version_goal', '')}".strip()[:200])
            vcs.tag(self.current_dir, f"v{version}", plan.get("version_goal", ""))
            if newly_completed:
                vcs.tag(self.current_dir, vcs.GOAL_TAG, f"Core user goal met at v{version}")

        # reports
        reporting.write_version_changelog(self.changelog_path, version, plan, diff, test_result, review, state.get("phase"))

        record = {
            "version": version,
            "phase": state.get("phase"),
            "completed_at": now_text(),
            "plan": plan,
            "diff": diff,
            "review_score": review.get("score", 0),
            "review_issues": review.get("issues", []),
            "feature_summary": review.get("feature_summary", ""),
            "whats_new": review.get("whats_new", []),
            "test_result": test_result,
            "goal_met": goal_met,
            "goal_progress": goal_progress,
            "commit": commit,
            "snapshot": str(version_dir.relative_to(self.root)),
        }
        state["current_version"] = version
        state["last_review"] = review
        state["last_test_result"] = test_result
        state.setdefault("versions", []).append(record)
        state["cost"] = self.cost
        reporting.write_features_overview(self.features_path, state)

        score = review.get("score", "?")
        _log(f"[v{version}] complete | score {score}/100 | tests "
             f"{'PASS' if test_result.get('success') else 'FAIL'} | goal {goal_progress}% | "
             f"+{len(diff['added'])}/~{len(diff['changed'])} files")
        self._emit(state, step="VERSION_DONE", agent="", message=f"v{version} score {score}")
        return state

    def _plan(self, version: int, state: dict[str, Any]) -> dict[str, Any]:
        phase = state.get("phase", "build")
        if phase == "build":
            guidance = ("Phase: BUILD. The original goal is NOT fully met yet. Drive directly toward "
                        "completing the user's requested product. Fix real bugs first, then add the "
                        "core features the user asked for.")
        else:
            guidance = ("Phase: EXPAND. The core goal is already met. Keep it working, then build the "
                        "most valuable accepted backlog item(s) that extend the product into useful "
                        "adjacent features. Every addition must be genuinely useful to this product.")
        context = collect_context(self.current_dir)
        previous = json.dumps({
            "last_review": {k: state.get("last_review", {}).get(k) for k in ("score", "issues", "suggestions_for_next_version")},
            "last_test_result": {"success": state.get("last_test_result", {}).get("success")},
            "recent_versions": [{"version": v.get("version"), "summary": v.get("feature_summary")}
                                for v in state.get("versions", [])[-3:]],
        }, ensure_ascii=False, indent=2)
        backlog = self._backlog_text(state)
        prompt = prompts.render_template(self.app_dir, "plan", {
            "version": version, "goal": state.get("goal", ""), "phase": phase,
            "architecture": read_text(self.architecture_path, "(architecture missing)"),
            "phase_guidance": guidance, "backlog": backlog, "previous": previous, "context": context,
        })
        raw = self._call(state, "PLAN", prompt, self.current_dir, step="PLAN", agent="AgentPLAN")
        plan = extract_json(raw, {
            "version_goal": f"Improve version {version}", "acceptance_criteria": [],
            "dev_agents": DEV_DEFAULT, "test_focus": [], "risks": [],
        })
        if not plan.get("dev_agents"):
            plan["dev_agents"] = DEV_DEFAULT
        save_json(self.plans_dir / f"v{version}.json", plan)
        return plan

    def _develop(self, version: int, state: dict[str, Any], plan: dict[str, Any], before_dir: Path) -> list[dict[str, Any]]:
        dev_agents = plan.get("dev_agents") or DEV_DEFAULT

        # Single dev agent: edit current/ directly so file changes are visible
        # live (no isolated workspace). Multiple agents still use isolated
        # workspaces + conflict-aware merge for safety.
        if len(dev_agents) == 1:
            agent = dev_agents[0]
            name = slugify(str(agent.get("name") or "AgentDEV"))
            owns = agent.get("owns") or []
            prompt = prompts.render_template(self.app_dir, "dev", {
                "agent_name": name, "version": version, "goal": state.get("goal", ""),
                "architecture": read_text(self.architecture_path, ""),
                "plan": json.dumps(plan, ensure_ascii=False, indent=2),
                "task": agent.get("task", ""),
                "owns": ", ".join(owns) if owns else "(not restricted)",
            })
            text = self._call(state, name, prompt, self.current_dir, step="DEV", agent=name)
            return [{"name": name, "role": agent.get("role", "dev"),
                     "workspace": str(self.current_dir), "output": text,
                     "files": list_generated_files(self.current_dir)}]

        specs = []
        for idx, agent in enumerate(dev_agents, start=1):
            name = slugify(str(agent.get("name") or f"AgentDEV_{idx}"))
            workspace = self.work_dir / f"v{version}" / name
            safe_rmtree(workspace, self.root)
            copy_tree_contents(self.current_dir, workspace)
            owns = agent.get("owns") or []
            prompt = prompts.render_template(self.app_dir, "dev", {
                "agent_name": name, "version": version, "goal": state.get("goal", ""),
                "architecture": read_text(self.architecture_path, ""),
                "plan": json.dumps(plan, ensure_ascii=False, indent=2),
                "task": agent.get("task", ""),
                "owns": ", ".join(owns) if owns else "(not restricted; avoid touching peers' files)",
            })
            specs.append({"name": name, "role": agent.get("role", "dev"), "workspace": workspace, "prompt": prompt})

        outputs: list[dict[str, Any]] = []
        if self.allow_parallel and len(specs) > 1:
            _log(f"[v{version}] Running {len(specs)} dev agents in parallel...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
                futures = {pool.submit(self._run_dev_spec, version, state, spec): spec for spec in specs}
                for future in concurrent.futures.as_completed(futures):
                    outputs.append(future.result())
        else:
            for spec in specs:
                outputs.append(self._run_dev_spec(version, state, spec))

        # Order outputs by original spec order for deterministic conflict resolution.
        order = {spec["name"]: i for i, spec in enumerate(specs)}
        outputs.sort(key=lambda o: order.get(o["name"], 0))
        self._merge_dev_outputs(version, state, before_dir, outputs)
        return outputs

    def _run_dev_spec(self, version: int, state: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        text = self._call(state, spec["name"], spec["prompt"], spec["workspace"],
                          step="DEV", agent=spec["name"])
        return {"name": spec["name"], "role": spec["role"], "workspace": str(spec["workspace"]),
                "output": text, "files": list_generated_files(spec["workspace"])}

    def _merge_dev_outputs(self, version: int, state: dict[str, Any], before_dir: Path,
                           outputs: list[dict[str, Any]]) -> None:
        """Merge only files each agent actually changed; first writer wins on conflict."""
        claimed: dict[str, str] = {}
        conflicts: list[str] = []
        for result in outputs:
            ws = Path(result["workspace"])
            for path in sorted(ws.rglob("*")):
                if path.is_dir():
                    continue
                rel = path.relative_to(ws)
                if any(part in INTERNAL_DIRS for part in rel.parts):
                    continue
                rel_str = rel.as_posix()
                if not _file_differs(path, before_dir / rel):
                    continue  # agent left this file unchanged; do not clobber peers
                if rel_str in claimed:
                    conflicts.append(f"{rel_str} (kept {claimed[rel_str]}, skipped {result['name']})")
                    continue
                claimed[rel_str] = result["name"]
                target = self.current_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        if conflicts:
            _log(f"[v{version}] ⚠ merge conflicts (first writer kept): {conflicts}")
            self._emit(state, step="MERGE", agent="", message=f"conflicts: {conflicts}")

    def _doc(self, version: int, state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        workspace = self.work_dir / f"v{version}" / "AgentDOC"
        safe_rmtree(workspace, self.root)
        copy_tree_contents(self.current_dir, workspace)
        prompt = prompts.render_template(self.app_dir, "doc", {
            "version": version, "goal": state.get("goal", ""),
            "plan": json.dumps(plan, ensure_ascii=False, indent=2),
        })
        text = self._call(state, "AgentDOC", prompt, workspace, step="DOC", agent="AgentDOC")
        for path in sorted(workspace.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(workspace)
            if any(part in INTERNAL_DIRS for part in rel.parts) or path.suffix.lower() not in DOC_SUFFIXES:
                continue
            target = self.current_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        return {"name": "AgentDOC", "role": "documentation", "workspace": str(workspace),
                "output": text, "files": list_generated_files(workspace)}

    def _test(self, version: int, state: dict[str, Any], plan: dict[str, Any], suffix: str = "") -> dict[str, Any]:
        label_suffix = f"_{suffix}" if suffix else ""
        candidates = testing.detect_candidates(self.current_dir)
        override = deep_get(self.config, "tests.command", "")
        if override:
            candidates.insert(0, {"name": "configured", "command": override, "kind": "configured"})

        if self.steps.get("test_agent"):
            prompt = prompts.render_template(self.app_dir, "test", {
                "version": version, "goal": state.get("goal", ""),
                "plan": json.dumps(plan, ensure_ascii=False, indent=2),
                "candidates": json.dumps(candidates, ensure_ascii=False, indent=2),
                "context": collect_context(self.current_dir, max_bytes=40_000),
            })
            raw = self._call(state, f"TEST{label_suffix}", prompt, self.current_dir, step="TEST", agent="AgentTEST")
            decision = extract_json(raw, {"commands": [], "reason": "fallback"})
            commands = [str(c) for c in decision.get("commands", []) if str(c).strip()]
        else:
            decision = {"commands": [], "reason": "built-in detection (simple mode)"}
            commands = []
            self._emit(state, step="TEST", agent="builtin", message="running built-in tests")

        if not commands:
            commands = [candidates[0]["command"]] if candidates else ["__builtin_file_smoke__"]

        results = []
        for command in commands:
            _log(f"[v{version}] [TEST] {command}")
            log_path = self.logs_dir / f"v{version}{label_suffix}_test_{slugify(command)[:30]}.log"
            results.append(testing.run_command(self.current_dir, command, self.test_timeout, log_path))
        success = all(r.get("success") for r in results) if results else False
        result = {"success": success, "decision": decision, "commands": commands, "results": results}
        save_json(self.tests_dir / f"v{version}{label_suffix}.json", result)
        return result

    def _review(self, version: int, state: dict[str, Any], plan: dict[str, Any],
                test_result: dict[str, Any], dev_outputs: list[dict[str, Any]], suffix: str = "") -> dict[str, Any]:
        summaries = [{k: v for k, v in o.items() if k != "output"} for o in dev_outputs]
        prompt = prompts.render_template(self.app_dir, "review", {
            "version": version, "goal": state.get("goal", ""), "phase": state.get("phase", "build"),
            "plan": json.dumps(plan, ensure_ascii=False, indent=2),
            "test_result": json.dumps(test_result, ensure_ascii=False, indent=2),
            "dev_summaries": json.dumps(summaries, ensure_ascii=False, indent=2),
            "context": collect_context(self.current_dir, max_bytes=50_000),
        })
        label = f"REVIEW_{suffix}" if suffix else "REVIEW"
        raw = self._call(state, label, prompt, self.current_dir, step="REVIEW", agent="AgentREVIEW")
        review = extract_json(raw, {
            "score": 70, "blocking": False, "goal_met": False, "goal_progress": 0,
            "issues": [], "good_points": [], "feature_summary": plan.get("version_goal", ""),
            "whats_new": [], "suggestions_for_next_version": [],
        })
        # Pin the numeric scale: tolerate models that return 0-1 fractions.
        review["score"] = _coerce_pct(review.get("score"), 0)
        review["goal_progress"] = _coerce_pct(review.get("goal_progress"), 0)
        save_json(self.reviews_dir / f"v{version}{('_' + suffix) if suffix else ''}.json", review)
        return review

    def _needs_fix(self, test_result: dict[str, Any], review: dict[str, Any]) -> bool:
        if not test_result.get("success"):
            return True
        if review.get("blocking"):
            return True
        score = review.get("score", 0)
        return isinstance(score, int) and score < self.review_threshold

    def _fix(self, version: int, state: dict[str, Any], plan: dict[str, Any],
             test_result: dict[str, Any], review: dict[str, Any], attempt: int) -> None:
        prompt = prompts.render_template(self.app_dir, "fix", {
            "version": version, "attempt": attempt, "goal": state.get("goal", ""),
            "plan": json.dumps(plan, ensure_ascii=False, indent=2),
            "test_result": json.dumps(test_result, ensure_ascii=False, indent=2),
            "review": json.dumps(review, ensure_ascii=False, indent=2),
        })
        self._call(state, f"FIX{attempt}", prompt, self.current_dir, step="FIX", agent="AgentFIX")

    def _assess_goal(self, version: int, state: dict[str, Any], review: dict[str, Any]) -> tuple[bool, int]:
        goal_met = bool(review.get("goal_met"))
        progress = _coerce_pct(review.get("goal_progress"), 0)
        if not self.steps.get("goal_check"):
            return goal_met, progress
        prompt = prompts.render_template(self.app_dir, "goal_check", {
            "goal": state.get("goal", ""),
            "review": json.dumps(review, ensure_ascii=False, indent=2),
            "context": collect_context(self.current_dir, max_bytes=40_000),
        })
        raw = self._call(state, "GOALCHECK", prompt, self.current_dir, step="GOAL_CHECK", agent="AgentGOALCHECK")
        decision = extract_json(raw, {"goal_met": goal_met, "goal_progress": progress})
        save_json(self.reviews_dir / f"v{version}_goalcheck.json", decision)
        return bool(decision.get("goal_met", goal_met)), _coerce_pct(decision.get("goal_progress", progress), progress)

    def _scout_and_evaluate(self, version: int, state: dict[str, Any], review: dict[str, Any]) -> None:
        scout_prompt = prompts.render_template(self.app_dir, "scout", {
            "version": version, "goal": state.get("goal", ""),
            "review": json.dumps(review, ensure_ascii=False, indent=2),
            "backlog": self._backlog_text(state),
            "context": collect_context(self.current_dir, max_bytes=35_000),
        })
        raw = self._call(state, "SCOUT", scout_prompt, self.current_dir, step="SCOUT", agent="AgentSCOUT")
        candidates = extract_json(raw, {"candidates": []}).get("candidates", [])
        if not candidates:
            return
        if self.steps.get("evaluate"):
            eval_prompt = prompts.render_template(self.app_dir, "evaluate", {
                "goal": state.get("goal", ""),
                "candidates": json.dumps(candidates, ensure_ascii=False, indent=2),
                "threshold": self.value_threshold,
            })
            eraw = self._call(state, "EVALUATE", eval_prompt, self.current_dir, step="EVALUATE", agent="AgentEVALUATE")
            evals = extract_json(eraw, {"evaluations": []}).get("evaluations", [])
            by_title = {str(e.get("title", "")).strip().lower(): e for e in evals}
        else:
            by_title = {}

        existing = {str(b.get("title", "")).strip().lower() for b in state.get("backlog", [])}
        for cand in candidates:
            title = str(cand.get("title", "")).strip()
            if not title or title.lower() in existing:
                continue
            ev = by_title.get(title.lower(), {})
            accepted = bool(ev.get("accepted")) if self.steps.get("evaluate") else True
            value = int(ev.get("value", 0) or 0)
            if self.steps.get("evaluate") and not accepted:
                status = "rejected"
            else:
                status = "accepted"
            state.setdefault("backlog", []).append({
                "title": title, "description": cand.get("description", ""),
                "value": value, "effort": ev.get("effort", ""), "status": status,
                "reason": ev.get("reason", cand.get("rationale", "")),
                "proposed_in_version": version,
            })
        save_json(self.app_dir / "backlog.json", {"backlog": state.get("backlog", [])}, stamp=False)

    def _backlog_text(self, state: dict[str, Any]) -> str:
        accepted = [b for b in state.get("backlog", []) if b.get("status") == "accepted"]
        if not accepted:
            return "(empty)"
        accepted.sort(key=lambda b: b.get("value", 0), reverse=True)
        return "\n".join(f"- [{b.get('value', 0)}] {b.get('title')}: {b.get('description', '')[:120]}"
                         for b in accepted[:12])
