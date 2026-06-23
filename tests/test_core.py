"""Lightweight unit tests for AutoDevLoop's pure helpers (no provider needed)."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
from pathlib import Path

from autodevloop import brainstorm, control, repair, webapp, yaml_compat
from autodevloop.config import default_config, load_config, resolved_steps, provider_invocation, provider_for_agent, save_config
from autodevloop.engine import AutoDevLoop
from autodevloop.llm import call
from autodevloop.prompts import DEFAULT_TEMPLATES, render
from autodevloop.util import extract_json, diff_file_lists, load_json, save_json, slugify


def test_yaml_round_trip():
    cfg = default_config()
    back = yaml_compat.load(yaml_compat.dump(cfg))
    assert back["pipeline"]["mode"] == "advanced"
    assert back["agents"]["allow_parallel"] is True
    assert back["review"]["threshold"] == 80


def test_modes_resolve_steps():
    cfg = default_config()
    simple = copy.deepcopy(cfg)
    simple["pipeline"]["mode"] = "simple"
    assert resolved_steps(simple)["scout"] is False
    assert resolved_steps(simple)["test_agent"] is False
    assert resolved_steps(cfg)["scout"] is True
    # per-step override on top of a mode
    cfg["pipeline"]["steps"] = {"doc": False}
    assert resolved_steps(cfg)["doc"] is False


def test_provider_invocation_defaults_to_claude_json():
    prof = provider_invocation(default_config())
    assert prof["command"] == "claude"
    assert prof["output"] == "claude-json"


def test_provider_can_be_assigned_per_agent_and_old_default_is_inherited():
    cfg = default_config()
    cfg["provider"]["name"] = "gemini"
    assert provider_for_agent(cfg, "dev")["name"] == "gemini"
    cfg["provider"]["assignments"]["review"] = "codex"
    cfg["provider"]["profiles"]["codex"]["command"] = "/custom/codex"
    review = provider_for_agent(cfg, "review")
    assert review["name"] == "codex"
    assert review["command"] == "/custom/codex"


def test_prompt_render_preserves_json_braces():
    out = render(DEFAULT_TEMPLATES["plan"], {
        "version": 3, "goal": "g", "phase": "build", "architecture": "A",
        "phase_guidance": "PG", "backlog": "B", "previous": "P", "context": "C",
    })
    assert "{{" not in out  # placeholders all replaced
    assert '"version_goal"' in out  # literal JSON braces preserved
    assert "v3" in out


def test_extract_json_from_fenced_and_noisy():
    assert extract_json('```json\n{"a": 1}\n```', {})["a"] == 1
    assert extract_json('prefix {"score": 88, "ok": true} suffix', {})["score"] == 88
    assert extract_json("no json here", {"fallback": 1}) == {"fallback": 1}


def test_slugify():
    assert slugify("AgentDEV/Core 1!") == "AgentDEV_Core_1"
    assert slugify("") == "item"


def test_diff_file_lists(tmp_path):
    before = tmp_path / "b"
    after = tmp_path / "a"
    before.mkdir()
    after.mkdir()
    (before / "keep.txt").write_text("same")
    (after / "keep.txt").write_text("same")
    (before / "gone.txt").write_text("x")
    (after / "new.txt").write_text("y")
    (before / "edit.txt").write_text("1")
    (after / "edit.txt").write_text("2")
    diff = diff_file_lists(before, after)
    assert diff["added"] == ["new.txt"]
    assert diff["removed"] == ["gone.txt"]
    assert diff["changed"] == ["edit.txt"]


def test_llm_call_can_pipe_prompt_to_provider_stdin():
    profile = {
        "command": sys.executable,
        "args": ["-c", "import sys; print(sys.stdin.read())"],
        "prompt_via": "stdin",
        "output": "text",
        "name": "test",
    }

    result = call(profile, "brainstorm prompt", Path.cwd(), retries=1)

    assert result.text.strip() == "brainstorm prompt"


def test_delete_project_removes_registered_directory(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "keep.txt").write_text("content")
    removed = []
    monkeypatch.setattr(webapp.registry, "load", lambda: [{"dir": str(root)}])
    monkeypatch.setattr(webapp.registry, "remove", lambda path: removed.append(path))

    result = webapp._delete_project({"dir": str(root), "confirm_dir": str(root)})

    assert result["ok"] is True
    assert not root.exists()
    assert removed == [root.resolve()]


def test_delete_project_refuses_running_project(tmp_path, monkeypatch):
    root = tmp_path / "running-project"
    root.mkdir()
    monkeypatch.setattr(webapp.registry, "load", lambda: [{"dir": str(root)}])
    monkeypatch.setattr(webapp, "_is_running", lambda _path: True)

    result = webapp._delete_project({"dir": str(root), "confirm_dir": str(root)})

    assert result["ok"] is False
    assert root.exists()


def test_delete_project_requires_exact_path_confirmation(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(webapp.registry, "load", lambda: [{"dir": str(root)}])

    result = webapp._delete_project({"dir": str(root), "confirm_dir": str(tmp_path)})

    assert result["ok"] is False
    assert root.exists()


def test_brainstorm_history_records_questions_answers_and_initial_design():
    history = brainstorm.render_history({
        "goal": "Build an editor",
        "transcript": [
            {"role": "assistant", "text": "Desktop or web?"},
            {"role": "user", "text": "Desktop"},
        ],
        "generated_spec": "# Design\nUse a desktop shell.",
    })

    assert "AI · Question 1" in history
    assert "User · Answer 1" in history
    assert "Initial AI proposal" in history


def test_brainstorm_design_is_editable_before_run_and_updates_build_goal(tmp_path, monkeypatch):
    root = tmp_path / "project"
    app_dir = root / ".autodev"
    app_dir.mkdir(parents=True)
    save_config(root, default_config())
    brainstorm.save_session(app_dir, {
        "goal": "rough goal", "transcript": [], "done": True,
        "refined_goal": "refined", "spec": "initial", "generated_spec": "initial",
        "arch_hint": "", "turns": 1,
    })
    monkeypatch.setattr(webapp, "_is_running", lambda _path: False)

    result = webapp._save_brainstorm_design({"dir": str(root), "spec": "edited build brief"})

    assert result["ok"] is True
    assert load_config(root)["project"]["goal"] == "edited build brief"
    assert (root / "docs" / "brainstorm-spec.md").read_text().strip() == "edited build brief"
    assert webapp._brainstorm_design(str(root))["editable"] is True


def test_brainstorm_design_becomes_read_only_after_run_state_exists(tmp_path, monkeypatch):
    root = tmp_path / "project"
    app_dir = root / ".autodev"
    app_dir.mkdir(parents=True)
    save_config(root, default_config())
    brainstorm.save_session(app_dir, {"goal": "g", "done": True, "spec": "locked"})
    (app_dir / "state.json").write_text('{"status": "stopped", "current_version": 0}')
    monkeypatch.setattr(webapp, "_is_running", lambda _path: False)

    design = webapp._brainstorm_design(str(root))
    result = webapp._save_brainstorm_design({"dir": str(root), "spec": "changed"})

    assert design["editable"] is False
    assert result["ok"] is False


def test_partial_web_settings_save_preserves_project_goal(tmp_path, monkeypatch):
    cfg = default_config()
    cfg["project"]["goal"] = "keep this build brief"
    save_config(tmp_path, cfg)
    monkeypatch.setattr(webapp, "_is_running", lambda _path: False)

    result = webapp._save_config(str(tmp_path), {"config": {"review": {"threshold": 91}}})

    saved = load_config(tmp_path)
    assert result["ok"] is True
    assert saved["project"]["goal"] == "keep this build brief"
    assert saved["review"]["threshold"] == 91


def test_directive_scopes_and_next_is_consumed_only_after_success(tmp_path):
    app_dir = tmp_path / ".autodev"
    next_item = control.add_directive(app_dir, "Use SQLite", "next", 4)
    control.add_directive(app_dir, "Keep API stable", "future", 2)
    assert "Use SQLite" in control.render_directives(app_dir, 4)
    assert "Keep API stable" in control.render_directives(app_dir, 8)
    control.mark_directives_applied(app_dir, 4, "DEV:AgentDEV")
    items = {item["id"]: item for item in control.load_directives(app_dir)}
    assert items[next_item["id"]]["active"] is False
    assert "DEV:AgentDEV" in items[next_item["id"]]["applied_to"]


def test_checkpoint_restore_recovers_previous_after_interrupted_directory_swap(tmp_path):
    root = tmp_path / "project"
    app_dir = root / ".autodev"
    source = root / "source"
    target = root / "target"
    source.mkdir(parents=True)
    (source / "ok.txt").write_text("complete")
    active = control.snapshot_active(app_dir, source)
    previous = active.with_name("active.previous")
    active.replace(previous)  # simulate interruption between the two atomic renames

    assert control.restore_active(app_dir, target) is True
    assert (target / "ok.txt").read_text() == "complete"


def test_web_pause_discards_inflight_files_and_keeps_next_agent(tmp_path, monkeypatch):
    root = tmp_path / "project"
    app_dir = root / ".autodev"
    current = root / "current"
    current.mkdir(parents=True)
    (current / "stable.txt").write_text("stable")
    control.snapshot_active(app_dir, current)
    (current / "partial.txt").write_text("discard me")
    control.save_checkpoint(app_dir, {
        "run_type": "development", "version": 4, "phase": "build",
        "status": "running", "next_agent": "AgentREVIEW", "completed_steps": ["AgentTEST"],
    })
    save_json(app_dir / "state.json", {"status": "running", "current_version": 3})
    monkeypatch.setattr(control, "terminate_process_tree", lambda *_args: True)

    result = webapp._pause_run({"dir": str(root)})

    assert result["ok"] is True
    assert not (current / "partial.txt").exists()
    assert control.load_checkpoint(app_dir)["next_agent"] == "AgentREVIEW"
    assert load_json(app_dir / "state.json", {})["status"] == "paused"


def test_engine_resumes_from_last_successful_agent_checkpoint(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    cfg = default_config()
    cfg["pipeline"]["mode"] = "simple"
    cfg["pipeline"]["steps"] = {"arch": False}
    cfg["vcs"]["git"] = False
    cfg["project"].update({"goal": "build it", "name": "demo", "max_versions": 1})
    save_config(root, cfg)
    (root / "current").mkdir()
    (root / "current" / "base.txt").write_text("base")
    monkeypatch.setattr("autodevloop.engine.registry.register", lambda *_args: None)
    calls = []
    fail_dev = {"value": True}

    def fake_call(self, state, label, prompt, cwd, step, agent):
        calls.append(label)
        if label == "PLAN":
            return '{"version_goal":"v1","acceptance_criteria":[],"dev_agents":[{"name":"AgentDEV","task":"build","owns":[]}],"test_focus":[],"risks":[]}'
        if label == "AgentDEV":
            (cwd / "partial.txt").write_text("partial")
            if fail_dev["value"]:
                fail_dev["value"] = False
                raise RuntimeError("interrupted")
            (cwd / "done.txt").write_text("done")
            return "SUMMARY: done"
        if label == "REVIEW":
            return '{"score":100,"blocking":false,"goal_met":true,"goal_progress":100,"issues":[],"good_points":[],"feature_summary":"done","whats_new":["done"],"suggestions_for_next_version":[]}'
        raise AssertionError(label)

    monkeypatch.setattr(AutoDevLoop, "_call", fake_call)
    with __import__("pytest").raises(RuntimeError):
        AutoDevLoop(root, cfg).run(reset=False, goal="build it", project_name="demo", max_versions=1)

    cp = control.load_checkpoint(root / ".autodev")
    assert cp["next_agent"] == "AgentDEV"
    assert cp["completed_steps"] == ["AgentPLAN"]
    assert not (root / "current" / "partial.txt").exists()

    AutoDevLoop(root, cfg).run(reset=False, goal="build it", project_name="demo", max_versions=1)
    assert calls.count("PLAN") == 1
    assert (root / "current" / "done.txt").exists()
    assert not control.load_checkpoint(root / ".autodev")
    assert load_json(root / ".autodev" / "state.json", {})["current_version"] == 1


def test_repair_branch_preserves_source_and_can_be_promoted(tmp_path):
    root = tmp_path / "project"
    source = root / "versions" / "v2"
    current = root / "current"
    source.mkdir(parents=True)
    current.mkdir(parents=True)
    (source / "app.txt").write_text("v2")
    (current / "app.txt").write_text("latest")
    job = repair.create_job(root, 2, "fix the bug")
    result = Path(job["result_dir"])
    (result / "app.txt").write_text("v2-fixed")
    job.update({"status": "completed", "accepted": True})
    repair.save_job(root, job)

    promoted = repair.promote_job(root, job["id"])

    assert promoted["ok"] is True
    assert (source / "app.txt").read_text() == "v2"
    assert (current / "app.txt").read_text() == "v2-fixed"
    assert Path(promoted["backup"], "app.txt").read_text() == "latest"


def test_process_tree_termination_prevents_late_file_write(tmp_path):
    if os.name == "nt":
        return
    root = tmp_path / "project"
    app_dir = root / ".autodev"
    app_dir.mkdir(parents=True)
    marker = root / "late.txt"
    proc = subprocess.Popen(
        ["bash", "-c", f"sleep 1; touch {marker}"],
        start_new_session=True,
    )
    control.write_run_control(app_dir, proc.pid, root)
    assert control.terminate_process_tree(proc, app_dir, root) is True
    time.sleep(1.2)
    assert not marker.exists()
