"""Human-readable reports: CHANGELOG, FEATURES overview table, final report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import markdown_list, now_text, read_text, write_text


def write_version_changelog(
    changelog_path: Path,
    version: int,
    plan: dict[str, Any],
    diff: dict[str, list[str]],
    test_result: dict[str, Any],
    review: dict[str, Any],
    phase: str,
) -> None:
    if not changelog_path.exists():
        write_text(changelog_path, "# Changelog\n\nAll AutoDevLoop-generated versions are recorded here.\n\n")

    whats_new = review.get("whats_new") or []
    summary = str(review.get("feature_summary") or plan.get("version_goal", "")).strip()
    commands = ", ".join(test_result.get("commands", [])) or "No command"
    lines = [
        f"## v{version} - {now_text()}  ({phase} phase)",
        "",
        f"_{summary}_" if summary else "",
        "",
        "### What's new",
        markdown_list(whats_new) if whats_new else markdown_list(diff.get("added", []) + diff.get("changed", [])),
        "",
        "### Files",
        f"- Added: {len(diff.get('added', []))}, Changed: {len(diff.get('changed', []))}, Removed: {len(diff.get('removed', []))}",
        "",
        "### Tests",
        f"- {'PASS' if test_result.get('success') else 'FAIL'}: {commands}",
        "",
        "### Known issues",
        markdown_list(review.get("issues", [])),
        "",
    ]
    existing = read_text(changelog_path)
    write_text(changelog_path, existing.rstrip() + "\n\n" + "\n".join(line for line in lines) + "\n")


def write_features_overview(features_path: Path, state: dict[str, Any]) -> None:
    """The at-a-glance table: every version, its features, and what changed."""
    versions = state.get("versions", [])
    goal_version = state.get("goal_completed_version")
    lines = [
        "# Features Overview",
        "",
        f"**Project:** {state.get('project_name', '')}  ",
        f"**Goal:** {state.get('goal', '')}",
        "",
    ]
    if goal_version:
        lines.append(f"> Core goal first fully met at **v{goal_version}** (tag `goal-complete`). "
                     "Later versions extend the product beyond the original request.")
        lines.append("")
    lines += [
        "| Version | Phase | Score | Tests | Summary | What's new |",
        "|---|---|---|---|---|---|",
    ]
    for item in versions:
        v = item.get("version")
        phase = item.get("phase", "build")
        score = item.get("review_score", "?")
        tests = "✅" if item.get("test_result", {}).get("success") else "❌"
        summary = _cell(item.get("feature_summary") or item.get("plan", {}).get("version_goal", ""))
        whats_new = _cell("; ".join(item.get("whats_new", [])[:4]))
        marker = " 🎯" if v == goal_version else ""
        lines.append(f"| v{v}{marker} | {phase} | {score}/100 | {tests} | {summary} | {whats_new} |")
    lines.append("")
    write_text(features_path, "\n".join(lines))


def _cell(text: str) -> str:
    flat = " ".join(str(text).split())
    flat = flat.replace("|", "\\|")
    return flat[:160] + ("…" if len(flat) > 160 else "")


def write_final_report(report_path: Path, state: dict[str, Any]) -> None:
    versions = state.get("versions", [])
    scores = [v.get("review_score", 0) for v in versions if isinstance(v.get("review_score", 0), int)]
    avg = round(sum(scores) / len(scores)) if scores else 0
    cost = state.get("cost", {})
    lines = [
        "# AutoDevLoop Final Report",
        "",
        f"**Project:** {state.get('project_name')}",
        f"**Status:** {state.get('status')}",
        f"**Stop reason:** {state.get('stop_reason', 'N/A')}",
        f"**Total versions:** {state.get('current_version')}",
        f"**Goal met at:** {('v' + str(state.get('goal_completed_version'))) if state.get('goal_completed_version') else 'not reached'}",
        f"**Average review score:** {avg}/100",
        f"**Estimated cost:** ${cost.get('cost_usd_total', 0):.4f} "
        f"(in {cost.get('input_tokens', 0)} / out {cost.get('output_tokens', 0)} tokens)",
        "",
        "## Goal",
        str(state.get("goal", "")),
        "",
        "## Version history",
    ]
    for item in versions:
        v = item.get("version")
        score = item.get("review_score", "?")
        test = item.get("test_result", {})
        phase = item.get("phase", "build")
        lines.append(
            f"- **v{v}** ({phase}) - score {score}/100 - tests "
            f"{'PASS' if test.get('success') else 'FAIL'} - {_cell(item.get('feature_summary', ''))}"
        )
    lines += [
        "",
        "## Output layout",
        "- Latest working copy: `current/`",
        "- Per-version snapshots: `versions/vN/`",
        "- Git history & tags inside `current/` (if git enabled)",
        "- Changelog: `CHANGELOG.md`",
        "- Features overview: `FEATURES.md`",
        "",
    ]
    write_text(report_path, "\n".join(lines))
