"""Tests for optional terminal colours in report rendering."""

from lastgreen.environment import Change
from lastgreen.newlines import NewLine
from lastgreen.report import Report, render_json, render_markdown, render_text


def make_report() -> Report:
    return Report(
        step="Run tests",
        changes=[
            Change(
                "runner",
                "runner image",
                "ubuntu-24.04 20260928.322.1",
                "ubuntu-24.04 20261001.1",
            )
        ],
        new_lines=[NewLine(42, "Error: fixture 'db' not found")],
    )


def test_text_output_has_no_colours_by_default() -> None:
    output = render_text(make_report())

    assert "\033[" not in output
    assert "20260928.322.1 → ubuntu-24.04 20261001.1" in output
    assert "Error: fixture 'db' not found" in output


def test_text_output_colours_version_change_green() -> None:
    output = render_text(make_report(), color=True)

    assert "\033[32m→ ubuntu-24.04 20261001.1\033[0m" in output


def test_text_output_colours_error_line_red() -> None:
    output = render_text(make_report(), color=True)

    assert "\033[31mError: fixture 'db' not found\033[0m" in output


def test_markdown_and_json_never_include_terminal_colours() -> None:
    report = make_report()

    assert "\033[" not in render_markdown(report)
    assert "\033[" not in render_json(report)
