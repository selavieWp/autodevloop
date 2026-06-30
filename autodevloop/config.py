"""Configuration defaults, loading, merging, and persistence."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from . import yaml_compat
from .util import CONFIG_FILE, read_text, write_text

DEFAULT_AGENT_TIMEOUT = 1800
DEFAULT_MAX_VERSIONS = 5
DEFAULT_REVIEW_THRESHOLD = 80
DEFAULT_FIX_RETRIES = 2
DEFAULT_MAX_PARALLEL_AGENTS = 3
DEFAULT_TEST_TIMEOUT = 120
DEFAULT_VALUE_THRESHOLD = 65

# Provider profiles. Only the CLI *command* (and optional args/model) differ;
# no API keys are ever requested. Users pre-configure their CLI locally.
PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    "claude": {
        "command": "claude",
        "args": [
            "--print", "--input-format", "text", "--output-format", "json",
            "--no-session-persistence", "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,MultiEdit,LS,Glob,Grep",
        ],
        "model_flag": "--model",
        "prompt_via": "stdin",
        "output": "claude-json",
    },
    "codex": {
        "command": "codex",
        "args": ["exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"],
        "model_flag": "--model",
        "prompt_via": "stdin",
        "output": "text",
    },
    "gemini": {
        "command": "gemini",
        "args": ["-y"],
        "model_flag": "--model",
        "prompt_via": "stdin",
        "output": "text",
    },
}

AGENT_PROVIDER_KEYS = (
    "brainstorm", "arch", "plan", "dev", "doc", "test", "review", "fix",
    "goal_check", "scout", "evaluate", "bugfix", "bugverify",
)

# Steps enabled per mode. Web/CLI may override individual steps.
SIMPLE_STEPS = {
    "arch": True,
    "goal_check": False,   # folded into review in simple mode
    "test_agent": False,   # built-in test detection only (no extra LLM call)
    "doc": False,
    "scout": False,        # no周边-feature scouting
    "evaluate": False,
    "features_doc": True,
}
ADVANCED_STEPS = {
    "arch": True,
    "goal_check": True,
    "test_agent": True,
    "doc": True,
    "scout": True,
    "evaluate": True,
    "features_doc": True,
}


def default_config() -> dict[str, Any]:
    return {
        "project": {
            "name": "",
            "goal": "",
            "max_versions": DEFAULT_MAX_VERSIONS,
            "arch_hint": "",
            "brainstorm": False,    # interactive design Q&A before the run
        },
        "provider": {
            "name": "claude",
            "command": "",          # blank -> use profile command
            "model": "",
            "extra_args": [],
            "profiles": {
                name: {"command": "", "model": "", "extra_args": []}
                for name in PROVIDER_PROFILES
            },
            # blank means inherit provider.name (keeps old single-provider configs)
            "assignments": {key: "" for key in AGENT_PROVIDER_KEYS},
        },
        "pipeline": {
            "mode": "advanced",     # "simple" | "advanced"
            "steps": {},            # per-step overrides on top of mode defaults
        },
        "agents": {
            "timeout": DEFAULT_AGENT_TIMEOUT,
            "allow_parallel": True,
            "max_parallel": DEFAULT_MAX_PARALLEL_AGENTS,
            "retries": 3,
            "backoff_seconds": 5,
        },
        "review": {
            "threshold": DEFAULT_REVIEW_THRESHOLD,
        },
        "value": {
            "threshold": DEFAULT_VALUE_THRESHOLD,
        },
        "fix": {
            "retries": DEFAULT_FIX_RETRIES,
        },
        "tests": {
            "timeout": DEFAULT_TEST_TIMEOUT,
            "command": "",
        },
        "vcs": {
            "git": True,
        },
    }


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(root: Path) -> dict[str, Any]:
    raw = yaml_compat.load(read_text(root / CONFIG_FILE))
    return deep_merge(default_config(), raw if isinstance(raw, dict) else {})


def save_config(root: Path, config: dict[str, Any]) -> None:
    merged = deep_merge(default_config(), config)
    write_text(root / CONFIG_FILE, yaml_compat.dump(merged))


def resolved_steps(config: dict[str, Any]) -> dict[str, bool]:
    mode = str(deep_get(config, "pipeline.mode", "advanced")).lower()
    base = ADVANCED_STEPS if mode == "advanced" else SIMPLE_STEPS
    steps = dict(base)
    overrides = deep_get(config, "pipeline.steps", {}) or {}
    if not isinstance(overrides, dict):
        overrides = {}
    for key, value in overrides.items():
        if key in steps and isinstance(value, bool):
            steps[key] = value
    return steps


def provider_invocation(config: dict[str, Any]) -> dict[str, Any]:
    return provider_for_agent(config, "")


def provider_for_agent(config: dict[str, Any], agent_key: str) -> dict[str, Any]:
    """Resolve a CLI profile for one pipeline role, preserving old configs."""
    default_name = str(deep_get(config, "provider.name", "claude")).lower()
    assigned = deep_get(config, f"provider.assignments.{agent_key}", "") if agent_key else ""
    name = str(assigned or default_name).lower()
    if name not in PROVIDER_PROFILES:
        name = default_name if default_name in PROVIDER_PROFILES else "claude"
    profile = copy.deepcopy(PROVIDER_PROFILES.get(name, PROVIDER_PROFILES["claude"]))
    configured = deep_get(config, f"provider.profiles.{name}", {}) or {}
    # Legacy command/model remain the default provider's overrides.
    legacy_command = deep_get(config, "provider.command", "") if name == default_name else ""
    legacy_model = deep_get(config, "provider.model", "") if name == default_name else ""
    legacy_extra = deep_get(config, "provider.extra_args", []) if name == default_name else []
    command = configured.get("command") or legacy_command or profile["command"]
    model = configured.get("model") or legacy_model or ""
    extra = configured.get("extra_args") or legacy_extra or []
    profile["command"] = command
    profile["extra_args"] = list(extra)
    profile["model"] = model
    profile["name"] = name
    return profile


def deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
