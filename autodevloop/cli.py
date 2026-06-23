"""Command-line interface for AutoDevLoop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__, control, repair
from .config import deep_get, deep_merge, load_config, save_config
from .engine import AutoDevLoop
from .util import APP_DIR, STATE_FILE, STOP_FILE, load_json, now_text, save_json, write_text


def resolve_project_dir(raw: str | None, base_dir: Path | None = None) -> Path:
    base = (base_dir or Path.cwd()).resolve()
    if not raw:
        return base
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def _prompt_int(label: str, default: int, minimum: int = 1) -> int:
    while True:
        raw = _prompt(label, str(default))
        try:
            value = int(raw)
        except ValueError:
            print(f"Please enter an integer >= {minimum}.")
            continue
        if value >= minimum:
            return value
        print(f"Please enter an integer >= {minimum}.")


def _prompt_multiline(label: str) -> str:
    print(label)
    print("Enter the requirement text. Finish with a single line containing only END.")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Build a config-shaped override dict from any explicitly-set CLI flags."""
    o: dict[str, Any] = {"project": {}, "provider": {}, "pipeline": {}, "agents": {}, "review": {}, "fix": {}, "tests": {}, "vcs": {}}
    if getattr(args, "project_name", ""):
        o["project"]["name"] = args.project_name
    if getattr(args, "arch_hint", ""):
        o["project"]["arch_hint"] = args.arch_hint
    if getattr(args, "mode", ""):
        o["pipeline"]["mode"] = args.mode
    if getattr(args, "provider", ""):
        o["provider"]["name"] = args.provider
    if getattr(args, "provider_command", ""):
        o["provider"]["command"] = args.provider_command
    if getattr(args, "model", ""):
        o["provider"]["model"] = args.model
    if getattr(args, "agent_timeout", None):
        o["agents"]["timeout"] = args.agent_timeout
    if getattr(args, "max_parallel_agents", None):
        o["agents"]["max_parallel"] = args.max_parallel_agents
    if getattr(args, "no_parallel", False):
        o["agents"]["allow_parallel"] = False
    if getattr(args, "review_threshold", None):
        o["review"]["threshold"] = args.review_threshold
    if getattr(args, "fix_retries", None) is not None:
        o["fix"]["retries"] = args.fix_retries
    if getattr(args, "test_command", ""):
        o["tests"]["command"] = args.test_command
    if getattr(args, "test_timeout", None):
        o["tests"]["timeout"] = args.test_timeout
    if getattr(args, "no_git", False):
        o["vcs"]["git"] = False
    if getattr(args, "brainstorm", False):
        o["project"]["brainstorm"] = True
    if getattr(args, "no_brainstorm", False):
        o["project"]["brainstorm"] = False
    return {k: v for k, v in o.items() if v}


def cmd_run(args: argparse.Namespace) -> None:
    execution_dir = Path.cwd().resolve()
    if not args.non_interactive and not args.project_dir:
        print("[AutoDevLoop] Project directory setup.")
        print(f"Absolute example: {execution_dir}")
        print("Relative example: . or demo-project")
        args.project_dir = _prompt("Project directory", str(execution_dir))

    root = resolve_project_dir(args.project_dir, execution_dir)
    config = deep_merge(load_config(root), _overrides_from_args(args))

    goal = args.goal or deep_get(config, "project.goal", "")
    project_name = deep_get(config, "project.name", "") or root.name
    max_versions = args.max_versions or int(deep_get(config, "project.max_versions", 5))

    if not args.non_interactive:
        print("[AutoDevLoop] Interactive setup. Press Enter to accept defaults.")
        if not project_name:
            project_name = _prompt("Project name", root.name)
        if not goal:
            goal = _prompt_multiline("Goal / user requirement")
        max_versions = _prompt_int("Max versions", max_versions, minimum=1)
        mode = _prompt("Mode (simple/advanced)", deep_get(config, "pipeline.mode", "advanced"))
        config = deep_merge(config, {"pipeline": {"mode": mode}, "project": {"name": project_name, "max_versions": max_versions}})

    if not goal and not (root / APP_DIR / STATE_FILE).exists():
        raise SystemExit("A goal is required. Use --goal \"...\" or run interactively.")

    # Optional interactive brainstorming: refine the goal into an agreed design
    # before the autonomous loop starts. Skipped in non-interactive mode.
    brainstorm_on = bool(deep_get(config, "project.brainstorm", False)) and not args.no_brainstorm
    if brainstorm_on and not args.non_interactive and goal:
        from . import brainstorm
        from .config import provider_for_agent
        refined_goal, arch_hint = brainstorm.run_cli_session(provider_for_agent(config, "brainstorm"), root, goal)
        goal = refined_goal or goal
        if arch_hint:
            existing_hint = deep_get(config, "project.arch_hint", "")
            merged_hint = f"{existing_hint}\n{arch_hint}".strip() if existing_hint else arch_hint
            config = deep_merge(config, {"project": {"arch_hint": merged_hint}})

    # Persist resolved config so the web UI and resumes see the same settings.
    save_config(root, deep_merge(config, {"project": {"name": project_name, "goal": goal, "max_versions": max_versions}}))

    print(f"[AutoDevLoop] Stop with: autodevloop stop --project-dir {root}")
    AutoDevLoop(root, config).run(
        reset=args.reset, goal=goal, project_name=project_name, max_versions=max_versions,
    )


def cmd_stop(args: argparse.Namespace) -> None:
    root = resolve_project_dir(args.project_dir)
    app_dir = root / APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    write_text(app_dir / STOP_FILE, f"stop requested at {now_text()}\n")
    print(f"[AutoDevLoop] Stop requested: {app_dir / STOP_FILE}")


def cmd_pause(args: argparse.Namespace) -> None:
    root = resolve_project_dir(args.project_dir)
    app_dir = root / APP_DIR
    cp = control.load_checkpoint(app_dir)
    if not cp:
        raise SystemExit("[AutoDevLoop] No resumable checkpoint found.")
    control.terminate_process_tree(None, app_dir, root)
    target = Path(cp.get("working_dir")) if cp.get("run_type") == "repair" and cp.get("working_dir") else root / "current"
    control.restore_active(app_dir, target)
    cp["status"] = "paused"
    cp["pause_reason"] = "Paused from CLI; in-flight agent output was discarded"
    control.save_checkpoint(app_dir, cp)
    state = load_json(app_dir / STATE_FILE, {})
    if cp.get("run_type") == "repair":
        job = repair.load_job(root, str(cp.get("job_id") or ""))
        if job:
            job["status"] = "paused"
            repair.save_job(root, job)
    else:
        state["status"] = "paused"
        state["stop_reason"] = cp["pause_reason"]
        save_json(app_dir / STATE_FILE, state)
        control.write_progress_doc(root, cp, state)
    print(f"[AutoDevLoop] Paused. Next agent: {cp.get('next_agent', 'unknown')}")


def cmd_resume(args: argparse.Namespace) -> None:
    root = resolve_project_dir(args.project_dir)
    cp = control.load_checkpoint(root / APP_DIR)
    if cp.get("run_type") == "repair":
        repair.run_job(root, str(cp.get("job_id") or ""))
        return
    config = load_config(root)
    state = load_json(root / APP_DIR / STATE_FILE, {})
    goal = deep_get(config, "project.goal", "") or state.get("goal", "")
    AutoDevLoop(root, config).run(
        reset=False, goal=goal,
        project_name=state.get("project_name") or deep_get(config, "project.name", "") or root.name,
        max_versions=int(state.get("max_versions") or deep_get(config, "project.max_versions", 5)),
    )


def cmd_repair_run(args: argparse.Namespace) -> None:
    root = resolve_project_dir(args.project_dir)
    repair.run_job(root, args.job_id)


def cmd_status(args: argparse.Namespace) -> None:
    root = resolve_project_dir(args.project_dir)
    state = load_json(root / APP_DIR / STATE_FILE, {})
    if not state:
        print("[AutoDevLoop] No state found. Run `autodevloop run` first.")
        return
    versions = state.get("versions", [])
    scores = [v.get("review_score", 0) for v in versions if isinstance(v.get("review_score", 0), int)]
    cost = state.get("cost", {})
    print()
    print(f"  Project : {state.get('project_name')}")
    print(f"  Status  : {state.get('status')}  (phase: {state.get('phase')})")
    print(f"  Version : v{state.get('current_version')} / {state.get('max_versions')}")
    print(f"  Goal    : {state.get('goal_progress', 0)}% met"
          + (f" (completed at v{state.get('goal_completed_version')})" if state.get('goal_completed_version') else ""))
    if scores:
        print(f"  Scores  : {scores} (avg {round(sum(scores)/len(scores))})")
    print(f"  Cost    : ${cost.get('cost_usd_total', 0):.4f} | "
          f"in {cost.get('input_tokens', 0)} / out {cost.get('output_tokens', 0)} tokens")
    print(f"  Updated : {state.get('updated_at')}")
    cp = control.load_checkpoint(root / APP_DIR)
    if cp:
        print(f"  Resume  : {cp.get('run_type')} v{cp.get('version')} | next {cp.get('next_agent')}")
    print()


def cmd_web(args: argparse.Namespace) -> None:
    from .webapp import serve
    serve(host=args.host, port=args.port)


def cmd_version(_args: argparse.Namespace) -> None:
    print(f"AutoDevLoop {__version__}")


def _add_project_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", default=None, help="Project directory (default: current dir).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodevloop", description="AI-driven autonomous development iteration loop.")
    parser.add_argument("--version", action="version", version=f"AutoDevLoop {__version__}")
    _add_project_dir(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Start or resume an autonomous run.")
    _add_project_dir(run)
    run.add_argument("--project-name", default="")
    run.add_argument("--goal", default="", help="User requirement / project goal.")
    run.add_argument("--max-versions", type=int, default=0, help="Number of versions to generate.")
    run.add_argument("--mode", choices=["simple", "advanced"], default="", help="Pipeline mode.")
    run.add_argument("--arch-hint", default="", help="Optional architecture / stack hint for AgentARCH.")
    run.add_argument("--provider", choices=["claude", "codex", "gemini"], default="", help="Provider profile.")
    run.add_argument("--provider-command", default="", help="Override the CLI command (e.g. a wrapper).")
    run.add_argument("--model", default="", help="Optional model alias/name passed to the provider.")
    run.add_argument("--test-command", default="", help="Override test command (otherwise auto-detected).")
    run.add_argument("--test-timeout", type=int, default=0)
    run.add_argument("--agent-timeout", type=int, default=0)
    run.add_argument("--review-threshold", type=int, default=0)
    run.add_argument("--fix-retries", type=int, default=None)
    run.add_argument("--max-parallel-agents", type=int, default=0)
    run.add_argument("--no-parallel", action="store_true")
    run.add_argument("--no-git", action="store_true", help="Disable git commits/tags in current/.")
    run.add_argument("--reset", action="store_true", help="Start fresh (wipe .autodev, versions, current).")
    run.add_argument("--non-interactive", action="store_true", help="Never prompt; fail if goal missing.")
    run.add_argument("--brainstorm", action="store_true", help="Refine the goal via an interactive design Q&A first.")
    run.add_argument("--no-brainstorm", action="store_true", help="Skip brainstorming even if enabled in config.")
    run.set_defaults(func=cmd_run)

    stop = sub.add_parser("stop", help="Request a running loop to stop.")
    _add_project_dir(stop)
    stop.set_defaults(func=cmd_stop)

    pause = sub.add_parser("pause", help="Immediately pause and discard only the in-flight agent.")
    _add_project_dir(pause)
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="Resume from the last successful agent checkpoint.")
    _add_project_dir(resume)
    resume.set_defaults(func=cmd_resume)

    repair_run = sub.add_parser("repair-run", help="Internal worker for a saved repair job.")
    _add_project_dir(repair_run)
    repair_run.add_argument("--job-id", required=True)
    repair_run.set_defaults(func=cmd_repair_run)

    status = sub.add_parser("status", help="Print run state summary.")
    _add_project_dir(status)
    status.set_defaults(func=cmd_status)

    web = sub.add_parser("web", help="Launch the local web dashboard.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8787)
    web.set_defaults(func=cmd_web)

    ver = sub.add_parser("version", help="Print version.")
    ver.set_defaults(func=cmd_version)
    return parser


def _make_stdout_safe() -> None:
    """Avoid UnicodeEncodeError on legacy Windows code pages (cp932/gbk/etc.)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_stdout_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
