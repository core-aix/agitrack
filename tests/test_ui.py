from prompt_toolkit.document import Document

from agitrack.shell.ui import _AgitrackCompleter


def test_agit_completer_provides_async_interface():
    completer = _AgitrackCompleter()

    assert hasattr(completer, "get_completions_async")


def test_agit_completer_suggests_colon_commands():
    completer = _AgitrackCompleter()

    completions = list(completer.get_completions(Document(":he"), None))

    assert [completion.text for completion in completions] == [":help"]


def test_agit_completer_suggests_slash_commands():
    completer = _AgitrackCompleter()

    completions = list(completer.get_completions(Document("/he"), None))

    assert [completion.text for completion in completions] == ["/help"]
