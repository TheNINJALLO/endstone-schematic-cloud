import importlib
import json
from types import SimpleNamespace

import pytest

from test_chunk_loading import _load_plugin_module


class Form:
    def __init__(self, **values):
        self.__dict__.update(values)
        self.buttons = {}

    def add_button(self, text, on_click):
        self.buttons[text] = on_click


@pytest.fixture
def ui(monkeypatch):
    _load_plugin_module()
    module = importlib.import_module("endstone_ninjos_schematics.forms")
    for name in ("ActionForm", "ModalForm"):
        monkeypatch.setattr(module, name, Form)
    for name in ("TextInput", "Toggle"):
        monkeypatch.setattr(module, name, lambda *args: args)
    shown, calls = [], []
    player = SimpleNamespace(unique_id="builder", send_form=shown.append, send_error_message=lambda e: pytest.fail(e))
    plugin = SimpleNamespace(
        require_schematic_access=lambda p: True,
        selections={"builder": SimpleNamespace(complete=True, size=(16, 8, 16))},
        config={},
        start_save=lambda *args: calls.append(("save", args)),
        request_move=lambda *args: calls.append(("move", args)),
        request_categories=lambda *args, **kwargs: calls.append(("categories", args, kwargs)),
        request_list=lambda *args, **kwargs: calls.append(("list", args, kwargs)),
        request_create_category=lambda *args: calls.append(("create", args)),
    )
    return module.SchematicForms(plugin), player, shown, calls


def test_save_destination_is_carried_through_selection_and_submission(ui):
    forms, player, shown, calls = ui
    forms.open_save(player)
    assert calls[-1] == ("categories", (player,), {"purpose": "save"})
    forms.show_categories(player, [{"name": "castles"}, {"name": "ships"}], "save")
    shown[-1].buttons["castles"](player)
    shown[-1].on_submit(player, json.dumps(["gate", "notes", True, False]))
    assert calls[-1] == ("save", (player, "gate", "notes", True, False, "castles"))


def test_move_chooses_correct_category_and_can_return_to_uncategorized(ui):
    forms, player, shown, calls = ui
    forms.show_categories(player, [{"name": "castles"}, {"name": "ships"}], "move", "gate")
    form = shown[-1]
    form.buttons["castles"](player)
    assert calls[-1] == ("move", (player, "gate", "castles"))
    form.buttons["Uncategorized"](player)
    assert calls[-1] == ("move", (player, "gate", ""))


def test_library_pagination_retains_category_and_search(ui):
    forms, player, shown, calls = ui
    rows = [dict(name=f"gate-{i}", size_x=1, size_y=1, size_z=1) for i in range(51)]
    forms.show_library(player, rows, "gate", "castles", 2)
    form = shown[-1]
    assert sum(label.startswith("gate-") for label in form.buttons) == 50
    form.buttons["Next Page"](player)
    assert calls[-1] == ("list", (player, "gate", "castles", 3), {})
    form.buttons["Previous Page"](player)
    assert calls[-1] == ("list", (player, "gate", "castles", 1), {})
    forms.show_library(player, [], category="new-empty-category")
    assert "Storage Categories" in shown[-1].buttons


def test_category_create_keeps_save_workflow(ui):
    forms, player, shown, calls = ui
    forms.open_create_category(player, "save")
    shown[-1].on_submit(player, json.dumps(["castles"]))
    assert calls[-1] == ("create", (player, "castles", "save", ""))


def test_category_database_actions_check_access_before_scheduling():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin.require_schematic_access = lambda _player: False
    plugin._submit_worker = lambda *_args: pytest.fail("unauthorized database operation")
    player = SimpleNamespace(unique_id="visitor")
    plugin.request_categories(player)
    plugin.request_create_category(player, "castles")
    plugin.request_move(player, "gate", "castles")
