from core import ui


def test_checkbox_noninteractive_default_empty():
    ui.set_mode(interactive=False, assume_yes=False)
    assert ui.checkbox("t", ["a", "b", "c"]) == []


def test_checkbox_noninteractive_assume_yes_all():
    ui.set_mode(interactive=False, assume_yes=True)
    assert ui.checkbox("t", ["a", "b", "c"]) == [0, 1, 2]


def test_checkbox_default_all_noninteractive():
    ui.set_mode(interactive=False, assume_yes=False)
    assert ui.checkbox("t", ["a", "b"], default_all=True) == [0, 1]


def test_checkbox_empty():
    ui.set_mode(interactive=False, assume_yes=False)
    assert ui.checkbox("t", []) == []
