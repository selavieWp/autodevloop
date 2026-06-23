"""Provider-agnostic LLM CLI invocation with retry, backoff, and cost tracking.

Only the CLI command differs between providers (claude / codex / gemini); no
API keys are handled here. Users authenticate their CLI of choice beforehand.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class LLMResult:
    text: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    raw: str = ""
    attempts: int = 1


@dataclass
class CallStats:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    by_label: dict[str, Any] = field(default_factory=dict)

    def add(self, label: str, result: LLMResult) -> None:
        self.cost_usd += result.cost_usd
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.calls += 1
        bucket = self.by_label.setdefault(label, {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0})
        bucket["cost_usd"] += result.cost_usd
        bucket["input_tokens"] += result.input_tokens
        bucket["output_tokens"] += result.output_tokens
        bucket["calls"] += 1


class TransientError(RuntimeError):
    pass


def _split_command(command: str) -> list[str]:
    if os.name == "nt":
        parts = [next(g for g in m if g) for m in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', command)]
    else:
        import shlex

        parts = shlex.split(command)
    return parts or ["claude"]


def resolve_command(command: str) -> list[str]:
    parts = _split_command(command)
    executable = parts[0]
    expanded = Path(executable).expanduser()
    if expanded.exists():
        parts[0] = str(expanded.resolve())
        return parts
    resolved = shutil.which(executable)
    if resolved:
        parts[0] = resolved
        return parts
    if os.name == "nt" and executable.lower() in {"claude", "claude.exe", "claude.cmd", "claude.bat"}:
        home = Path.home()
        for candidate in [
            home / ".local" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude.cmd",
            home / "AppData" / "Roaming" / "npm" / "claude.cmd",
            home / "AppData" / "Roaming" / "npm" / "claude.exe",
        ]:
            if candidate.exists():
                parts[0] = str(candidate.resolve())
                return parts
    return parts


def _parse_claude_json(stdout: str) -> tuple[str, float, int, int]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, 0.0, 0, 0
    if not isinstance(data, dict):
        return stdout, 0.0, 0, 0
    text = data.get("result") or data.get("text") or ""
    cost = float(data.get("total_cost_usd") or data.get("cost_usd") or 0.0)
    usage = data.get("usage") or {}
    in_tok = int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_read_input_tokens", 0) or 0) + int(usage.get("cache_creation_input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    return text if isinstance(text, str) else stdout, cost, in_tok, out_tok


_TRANSIENT_PATTERNS = (
    "overloaded", "rate limit", "rate_limit", "429", "503", "502", "500",
    "timeout", "timed out", "connection reset", "temporarily", "try again",
    "econnreset", "etimedout", "service unavailable",
)


def _looks_transient(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _TRANSIENT_PATTERNS)


def call(
    profile: dict[str, Any],
    prompt: str,
    cwd: Path,
    *,
    label: str = "LLM",
    timeout: int = 1800,
    retries: int = 3,
    backoff_seconds: float = 5.0,
    debug_file: Path | None = None,
    on_status: Callable[[str], None] | None = None,
) -> LLMResult:
    """Invoke the configured provider CLI once, with retry on transient errors."""
    last_error = ""
    for attempt in range(1, max(1, retries) + 1):
        try:
            result = _invoke_once(
                profile, prompt, cwd, label=label, timeout=timeout,
                debug_file=debug_file, on_status=on_status,
            )
            result.attempts = attempt
            return result
        except TransientError as exc:
            last_error = str(exc)
            if attempt >= retries:
                break
            delay = backoff_seconds * (2 ** (attempt - 1)) + random.uniform(0, backoff_seconds)
            if on_status:
                on_status(f"transient failure (attempt {attempt}/{retries}); retrying in {int(delay)}s")
            time.sleep(delay)
    raise RuntimeError(f"{label}: provider call failed after {retries} attempts. Last error:\n{last_error}")


def _invoke_once(
    profile: dict[str, Any],
    prompt: str,
    cwd: Path,
    *,
    label: str,
    timeout: int,
    debug_file: Path | None,
    on_status: Callable[[str], None] | None,
) -> LLMResult:
    command = resolve_command(str(profile.get("command") or "claude"))
    args = list(profile.get("args") or [])
    full = [*command, *args]
    model = profile.get("model")
    if model:
        full.extend([str(profile.get("model_flag") or "--model"), str(model)])
    full.extend(list(profile.get("extra_args") or []))
    if debug_file is not None and profile.get("name") == "claude":
        full.extend(["--debug-file", str(debug_file)])

    prompt_via = str(profile.get("prompt_via", "stdin"))
    use_stdin = prompt_via == "stdin"
    if not use_stdin:
        full.append(prompt)

    start = time.time()
    try:
        process = subprocess.Popen(
            full,
            cwd=str(cwd),
            stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Provider command not found: {profile.get('command')!r}. Install the CLI "
            "and add it to PATH, or change the provider command in settings."
        ) from exc

    stdout_chunks: list[str] = []
    stderr_lines: list[str] = []
    last_activity = [time.time()]

    def drain_stdout() -> None:
        assert process.stdout is not None
        while chunk := process.stdout.read(8192):
            stdout_chunks.append(chunk)
            last_activity[0] = time.time()

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stripped = line.rstrip()
            stderr_lines.append(stripped)
            if stripped:
                last_activity[0] = time.time()
                if on_status and any(kw in stripped.lower() for kw in ("write", "edit", "read", "tool", "error", "warn", "create")):
                    on_status(stripped[:160])

    def feed_stdin() -> None:
        if not use_stdin or process.stdin is None:
            return
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    # No periodic heartbeat events: the dashboard shows a live per-agent timer
    # client-side. We only surface meaningful tool-activity lines via on_status.
    threads = [
        threading.Thread(target=drain_stdout, daemon=True),
        threading.Thread(target=drain_stderr, daemon=True),
        threading.Thread(target=feed_stdin, daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise TransientError(f"{label}: provider timed out after {timeout}s")
    finally:
        # Once the child exits, both pipes reach EOF. Wait for the drainers so
        # parsing and error reporting always see the complete provider output.
        for thread in threads:
            thread.join()

    stdout = "".join(stdout_chunks)

    stderr_text = "\n".join(stderr_lines)
    if process.returncode != 0:
        tail = "\n".join(stderr_lines[-20:])
        if _looks_transient(stderr_text):
            raise TransientError(f"{label}: exit {process.returncode}. {tail}")
        raise RuntimeError(f"{label}: provider exited with code {process.returncode}.\n{tail}")

    duration = time.time() - start
    if profile.get("output") == "claude-json":
        text, cost, in_tok, out_tok = _parse_claude_json(stdout)
    else:
        text, cost, in_tok, out_tok = stdout, 0.0, 0, 0

    if not text.strip() and _looks_transient(stderr_text):
        raise TransientError(f"{label}: empty result with transient signal")

    return LLMResult(
        text=text,
        cost_usd=cost,
        input_tokens=in_tok,
        output_tokens=out_tok,
        duration_s=duration,
        raw=stdout,
    )
