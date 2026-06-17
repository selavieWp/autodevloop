"""Lightweight unit tests for AutoDevLoop's pure helpers (no provider needed)."""

from __future__ import annotations

import copy

from autodevloop import yaml_compat
from autodevloop.config import default_config, resolved_steps, provider_invocation
from autodevloop.prompts import DEFAULT_TEMPLATES, render
from autodevloop.util import extract_json, diff_file_lists, slugify


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
