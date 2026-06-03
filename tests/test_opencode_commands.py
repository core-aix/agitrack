from pathlib import Path

from agit.backends.opencode import OpenCodeBackend


def test_split_slash_command():
    backend = OpenCodeBackend(Path("."))

    command, args = backend._split_slash_command("/model openai/gpt-5.5")

    assert command == "model"
    assert args == ["openai/gpt-5.5"]


def test_split_empty_slash_command():
    backend = OpenCodeBackend(Path("."))

    command, args = backend._split_slash_command("/")

    assert command is None
    assert args == []
