"""Tests for terminal colour detection."""

import pytest

from lastgreen import cli


class FakeStream:
    """A fake output stream with configurable terminal support."""

    def __init__(self, is_terminal: bool) -> None:
        self.is_terminal = is_terminal

    def isatty(self) -> bool:
        return self.is_terminal


def test_color_enabled_for_tty_without_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert cli._should_use_color(FakeStream(True)) is True


def test_color_enabled_for_tty_with_empty_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "")

    assert cli._should_use_color(FakeStream(True)) is True


def test_color_disabled_when_no_color_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    assert cli._should_use_color(FakeStream(True)) is False


def test_color_disabled_when_output_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert cli._should_use_color(FakeStream(False)) is False
