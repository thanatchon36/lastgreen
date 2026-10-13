"""The comparison result and its text, Markdown, and JSON renderings."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from lastgreen.environment import Change
from lastgreen.newlines import NewLine


@dataclass
class RunInfo:
    run_id: int
    attempt: int
    job: str
    sha: str
    branch: str
    created: str  # ISO 8601
    url: str


@dataclass
class CodeChange:
    status: str  # "identical", "ahead", "behind", or "diverged" (GitHub compare API)
    ahead: int
    behind: int
    commits: list[tuple[str, str]]  # (short sha, subject), oldest first
    notable_files: list[str]
    url: str


@dataclass
class Report:
    step: str
    changes: list[Change]
    new_lines: list[NewLine]
    red: RunInfo | None = None
    green: RunInfo | None = None
    code: CodeChange | None = None
    notes: list[str] = field(default_factory=list)


_KIND_LABEL = {"runner": "", "action": "action ", "tool": "", "package": "package "}
SHOWN_COMMITS = 5
_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _paint(text: str, color_code: str, enabled: bool) -> str:
    """Apply terminal colour only when explicitly enabled."""
    return f"{color_code}{text}{_RESET}" if enabled else text


def _change_row(change: Change) -> tuple[str, str]:
    name = _KIND_LABEL[change.kind] + change.name
    if change.old is None:
        values = f"added {change.new}"
    elif change.new is None:
        values = f"removed {change.old}"
    else:
        values = f"{change.old} → {change.new}"
    if change.also:
        values += f"  (+{change.also} more line{'s' if change.also > 1 else ''})"
    return name, values


def _run_line(label: str, run: RunInfo) -> str:
    when = run.created.replace("T", " ")[:16]
    return f"{label:<6} run {run.run_id} attempt {run.attempt}  {run.job}  {run.sha[:7]}  {when}"


def _code_summary(code: CodeChange) -> str:
    if code.status == "identical":
        return "same commit: the code did not change"
    if code.status == "ahead":
        return f"{code.ahead} commit{'s' if code.ahead != 1 else ''} between green and red"
    if code.status == "behind":
        return f"red is {code.behind} commit{'s' if code.behind != 1 else ''} behind green"
    return f"red is {code.ahead} ahead of and {code.behind} behind green"


LINE_WIDTH = 200


def _clip(text: str) -> str:
    return text if len(text) <= LINE_WIDTH else text[: LINE_WIDTH - 1] + "…"


def render_text(report: Report, max_lines: int = 30, color: bool = False) -> str:
    out: list[str] = []
    if report.red:
        out.append(_run_line("red", report.red))
    if report.green:
        out.append(_run_line("green", report.green))
    if report.code:
        out.append(f"code   {_code_summary(report.code)}")
        shown = report.code.commits[-SHOWN_COMMITS:]
        hidden = len(report.code.commits) - len(shown)
        if hidden:
            out.append(f"         … {hidden} earlier")
        out.extend(f"         {sha} {subject}" for sha, subject in shown)
        if report.code.notable_files:
            out.append(f"         changed: {', '.join(report.code.notable_files)}")
    out.extend(f"note   {note}" for note in report.notes)
    if out:
        out.append("")

    if report.changes:
        out.append(f"Environment changes ({len(report.changes)})")
        rows = [_change_row(c) for c in report.changes]
        width = min(max(len(name) for name, _ in rows), 44)
        for name, values in rows:
            if color and " → " in values:
                arrow_pos = values.index(" → ")
                prefix = values[:arrow_pos]
                arrow_and_new = values[arrow_pos + 1 :]  # "→ <new>"
                coloured_values = prefix + " " + _paint(arrow_and_new, _GREEN, color)
            else:
                coloured_values = values
            out.append(f"  {name:<{width}}  {coloured_values}")
    else:
        out.append("Environment changes: none found")
    out.append("")

    shown_lines = report.new_lines[:max_lines]
    count = len(report.new_lines)
    if not count:
        out.append(f'New lines in failing step "{report.step}": none')
    else:
        of = f"first {len(shown_lines)} of {count}" if count > len(shown_lines) else str(count)
        out.append(f'New lines in failing step "{report.step}" ({of})')
        width = len(str(shown_lines[-1].line))
        for new in shown_lines:
            repeat = f"  (x{new.repeats})" if new.repeats > 1 else ""
            clipped = _clip(new.text)
            coloured_text = (
                _paint(clipped, _RED, color) if color and clipped.startswith("Error:") else clipped
            )
            out.append(f"  {new.line:>{width}}  {coloured_text}{repeat}")
    return "\n".join(out) + "\n"


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def render_markdown(report: Report, max_lines: int = 30) -> str:
    out: list[str] = ["## lastgreen", ""]
    if report.red and report.green:
        out += ["| | run | job | commit | started |", "|---|---|---|---|---|"]
        for label, run in (("red", report.red), ("green", report.green)):
            out.append(
                f"| {label} | [{run.run_id} attempt {run.attempt}]({run.url}) | "
                f"{_md_escape(run.job)} | `{run.sha[:7]}` | {run.created} |"
            )
        out.append("")
    if report.code:
        out.append(f"**Code:** [{_code_summary(report.code)}]({report.code.url})")
        out.extend(
            f"- `{sha}` {_md_escape(subject)}"
            for sha, subject in report.code.commits[-SHOWN_COMMITS:]
        )
        if report.code.notable_files:
            files = ", ".join(f"`{f}`" for f in report.code.notable_files)
            out.append(f"- changed: {files}")
        out.append("")
    out.extend(f"> {note}" for note in report.notes)
    if report.notes:
        out.append("")

    out.append(f"### Environment changes ({len(report.changes)})")
    out.append("")
    if report.changes:
        out += ["| what | change |", "|---|---|"]
        for change in report.changes:
            name, values = _change_row(change)
            out.append(f"| {_md_escape(name)} | {_md_escape(values)} |")
    else:
        out.append("None found.")
    out.append("")

    out.append(f"### New lines in failing step `{report.step}` ({len(report.new_lines)})")
    out.append("")
    if report.new_lines:
        out.append("```text")
        for new in report.new_lines[:max_lines]:
            repeat = f"  (x{new.repeats})" if new.repeats > 1 else ""
            out.append(f"{new.line:>6}  {_clip(new.text)}{repeat}")
        if len(report.new_lines) > max_lines:
            out.append(f"… {len(report.new_lines) - max_lines} more")
        out.append("```")
    else:
        out.append("None.")
    return "\n".join(out) + "\n"


def render_json(report: Report) -> str:
    return json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n"
