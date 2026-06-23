"""Interactive brainstorming: turn a rough goal into an agreed design before a run.

The provider CLI is invoked one-shot per turn (no session memory), so the full
Q&A transcript is kept in ``.autodev/brainstorm.json`` and fed back into every
prompt. The same core (:func:`next_turn`) powers both the CLI loop and the web
endpoint — only the I/O surface differs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import llm, prompts
from .util import APP_DIR, collect_context, extract_json, load_json, save_json, write_text

BRAINSTORM_FILE = "brainstorm.json"
BRAINSTORM_HISTORY_FILE = "brainstorm-history.md"
BRAINSTORM_SPEC_FILE = "brainstorm-spec.md"
MAX_TURNS = 12


def session_path(app_dir: Path) -> Path:
    return app_dir / BRAINSTORM_FILE


def load_session(app_dir: Path, goal: str = "") -> dict[str, Any]:
    data = load_json(session_path(app_dir), {})
    if not isinstance(data, dict) or not data:
        data = {
            "goal": goal, "transcript": [], "done": False,
            "refined_goal": "", "spec": "", "arch_hint": "", "turns": 0,
        }
    return data


def save_session(app_dir: Path, data: dict[str, Any]) -> None:
    save_json(session_path(app_dir), data)


def _render_transcript(transcript: list[dict[str, Any]]) -> str:
    if not transcript:
        return "(no questions asked yet)"
    lines = []
    for turn in transcript:
        role = "AI" if turn.get("role") == "assistant" else "User"
        lines.append(f"{role}: {str(turn.get('text', '')).strip()}")
    return "\n".join(lines)


def next_turn(provider: dict[str, Any], root: Path, app_dir: Path,
              session: dict[str, Any], *, max_turns: int = MAX_TURNS) -> dict[str, Any]:
    """Run one brainstorming turn (one LLM call). Mutates and persists ``session``.

    Returns the parsed model reply: either ``{"done": False, "question": ...}`` or
    ``{"done": True, "refined_goal": ..., "spec": ..., "arch_hint": ...}``.
    """
    transcript = session.get("transcript", [])
    context = collect_context(root) if root.exists() else "(empty)"
    prompt = prompts.render_template(app_dir, "brainstorm", {
        "goal": session.get("goal", ""),
        "transcript": _render_transcript(transcript),
        "context": context,
    })
    result = llm.call(provider, prompt, cwd=app_dir, label="BRAINSTORM")
    reply = extract_json(
        result.text,
        {"done": True, "refined_goal": "", "spec": "", "arch_hint": ""},
    )

    session["turns"] = int(session.get("turns", 0)) + 1
    forced = session["turns"] >= max_turns

    if reply.get("done") or forced:
        session["done"] = True
        session["refined_goal"] = str(reply.get("refined_goal") or session.get("goal", "")).strip()
        session["spec"] = str(reply.get("spec") or "").strip()
        session["generated_spec"] = session["spec"]
        session["arch_hint"] = str(reply.get("arch_hint") or "").strip()
        reply = {
            "done": True, "refined_goal": session["refined_goal"],
            "spec": session["spec"], "arch_hint": session["arch_hint"],
        }
    else:
        question = str(reply.get("question") or "").strip()
        session.setdefault("transcript", []).append({"role": "assistant", "text": question})
        reply["done"] = False

    save_session(app_dir, session)
    return reply


def record_reply(app_dir: Path, session: dict[str, Any], reply_text: str) -> None:
    """Append the user's answer to the transcript and persist."""
    session.setdefault("transcript", []).append({"role": "user", "text": str(reply_text).strip()})
    save_session(app_dir, session)


def finalize(root: Path, session: dict[str, Any]) -> tuple[str, str, str]:
    """Write the design and read-only conversation history to ``docs/``."""
    spec = str(session.get("spec") or "").strip()
    refined_goal = str(session.get("refined_goal") or session.get("goal", "")).strip()
    arch_hint = str(session.get("arch_hint") or "").strip()
    if spec:
        write_text(root / "docs" / BRAINSTORM_SPEC_FILE, spec + "\n")
    history = render_history(session)
    if history:
        write_text(root / "docs" / BRAINSTORM_HISTORY_FILE, history)
    return refined_goal, spec, arch_hint


def render_history(session: dict[str, Any]) -> str:
    """Render a stable Markdown record of the Q&A and initial AI proposal."""
    transcript = session.get("transcript") or []
    initial_spec = str(session.get("generated_spec") or session.get("spec") or "").strip()
    if not transcript and not initial_spec:
        return ""

    lines = [
        "# Brainstorm history",
        "",
        "> Read-only record of the design conversation before development began.",
        "",
    ]
    goal = str(session.get("goal") or "").strip()
    if goal:
        lines.extend(["## Original goal", "", goal, ""])
    if transcript:
        lines.extend(["## Conversation", ""])
        ai_no = user_no = 0
        for turn in transcript:
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            if turn.get("role") == "assistant":
                ai_no += 1
                heading = f"### AI · Question {ai_no}"
            else:
                user_no += 1
                heading = f"### User · Answer {user_no}"
            lines.extend([heading, "", text, ""])
    if initial_spec:
        lines.extend(["## Initial AI proposal", "", initial_spec, ""])
    return "\n".join(lines).rstrip() + "\n"


def run_cli_session(provider: dict[str, Any], root: Path, goal: str) -> tuple[str, str]:
    """Drive an interactive terminal brainstorming session.

    Returns ``(refined_goal, arch_hint)``. The refined goal replaces the run goal.
    Returns the original goal unchanged if the user cancels with ``/skip``.
    """
    app_dir = root / APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    session = load_session(app_dir, goal)
    session["goal"] = session.get("goal") or goal

    if session.get("done"):
        refined_goal, _spec, arch_hint = finalize(root, session)
        print("[AutoDevLoop] Reusing the design agreed in a previous brainstorm.")
        return refined_goal or goal, arch_hint

    print("\n[AutoDevLoop] Brainstorming mode. The AI asks one question at a time.")
    print("Answer each one; type /done to wrap up early, /skip to cancel brainstorming.\n")

    while not session.get("done"):
        reply = next_turn(provider, root, app_dir, session)
        if reply.get("done"):
            break
        question = reply.get("question", "")
        print(f"\nQ{session.get('turns')}: {question}")
        choices = reply.get("choices") or []
        for idx, choice in enumerate(choices, 1):
            print(f"   {idx}) {choice}")
        answer = input("> ").strip()
        if answer == "/skip":
            print("[AutoDevLoop] Brainstorming cancelled; using the original goal.")
            return goal, ""
        if answer == "/done":
            record_reply(app_dir, session,
                         "I'm done answering. Please finalise the design now with what we have.")
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            answer = choices[int(answer) - 1]
        record_reply(app_dir, session, answer)

    refined_goal, spec, arch_hint = finalize(root, session)
    print("\n[AutoDevLoop] Design agreed.\n")
    print(spec or refined_goal)
    if spec:
        print("\n[AutoDevLoop] Saved design to docs/brainstorm-spec.md")
    print()
    return refined_goal or goal, arch_hint
