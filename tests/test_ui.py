import asyncio

from core import ui


def _cb(*a, **kw):
    return asyncio.run(ui.checkbox(*a, **kw))


def test_checkbox_noninteractive_default_empty():
    ui.set_mode(interactive=False, assume_yes=False)
    assert _cb("t", ["a", "b", "c"]) == []


def test_checkbox_noninteractive_assume_yes_all():
    ui.set_mode(interactive=False, assume_yes=True)
    assert _cb("t", ["a", "b", "c"]) == [0, 1, 2]


def test_checkbox_default_all_noninteractive():
    ui.set_mode(interactive=False, assume_yes=False)
    assert _cb("t", ["a", "b"], default_all=True) == [0, 1]


def test_checkbox_empty():
    ui.set_mode(interactive=False, assume_yes=False)
    assert _cb("t", []) == []