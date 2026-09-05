import sys
import types
from pathlib import Path


def _load_listener_namespace():
    class Event:
        pass

    class PlayerInteractEvent(Event):
        LEFT_CLICK_BLOCK = 0
        RIGHT_CLICK_BLOCK = 1
        LEFT_CLICK_AIR = 2
        RIGHT_CLICK_AIR = 3

    class PlayerQuitEvent(Event):
        pass

    def event_handler(func):
        func._is_event_handler = True
        return func

    fake_endstone = types.ModuleType("endstone")
    fake_event = types.ModuleType("endstone.event")
    fake_event.Event = Event
    fake_event.PlayerInteractEvent = PlayerInteractEvent
    fake_event.PlayerQuitEvent = PlayerQuitEvent
    fake_event.event_handler = event_handler

    old_endstone = sys.modules.get("endstone")
    old_event = sys.modules.get("endstone.event")
    sys.modules["endstone"] = fake_endstone
    sys.modules["endstone.event"] = fake_event
    try:
        namespace = {}
        source = (
            Path(__file__).parents[1]
            / "src"
            / "endstone_ninjos_schematics"
            / "listener.py"
        ).read_text(encoding="utf-8")
        exec(compile(source, "listener.py", "exec"), namespace)
        return namespace
    finally:
        if old_endstone is None:
            sys.modules.pop("endstone", None)
        else:
            sys.modules["endstone"] = old_endstone
        if old_event is None:
            sys.modules.pop("endstone.event", None)
        else:
            sys.modules["endstone.event"] = old_event


class Player:
    def __init__(self, unique_id):
        self.unique_id = unique_id


def test_same_tool_operation_is_debounced_across_duplicate_events():
    namespace = _load_listener_namespace()
    claim = namespace["claim_interaction"]
    player = Player("abc")
    assert claim(player, "ninjos:schem_tablet", "menu", 0.45, now=10.0)
    assert not claim(player, "ninjos:schem_tablet", "menu", 0.45, now=10.01)
    assert not claim(player, "ninjos:schem_tablet", "menu", 0.45, now=10.44)
    assert claim(player, "ninjos:schem_tablet", "menu", 0.45, now=10.46)


def test_different_operations_or_players_are_not_collapsed():
    namespace = _load_listener_namespace()
    claim = namespace["claim_interaction"]
    first = Player("first")
    second = Player("second")
    assert claim(first, "ninjos:schem_selector", "selector_pos1", 0.45, now=20.0)
    assert claim(first, "ninjos:schem_selector", "selector_pos2", 0.45, now=20.0)
    assert claim(second, "ninjos:schem_selector", "selector_pos1", 0.45, now=20.0)


def test_listener_executes_tablet_only_once_for_eight_events():
    namespace = _load_listener_namespace()
    EventType = namespace["PlayerInteractEvent"]

    class Forms:
        def __init__(self):
            self.opens = 0

        def open_main(self, player):
            self.opens += 1

    class Plugin:
        tool_ids = {
            "selector": "ninjos:schem_selector",
            "placer": "ninjos:schem_placer",
            "rotator": "ninjos:schem_rotator",
            "tablet": "ninjos:schem_tablet",
            "undo": "ninjos:schem_undo",
            "redo": "ninjos:schem_redo",
            "confirm": "ninjos:schem_confirm",
        }
        _tool_debounce_seconds = 0.45

        def __init__(self):
            self.forms = Forms()

        @staticmethod
        def item_identifier(item):
            return item

        @staticmethod
        def require_schematic_access(player):
            return True

    class LivePlayer(Player):
        def has_permission(self, permission):
            return True

        def send_error_message(self, message):
            raise AssertionError(message)

    class Event:
        has_item = True
        item = "ninjos:schem_tablet"
        has_block = True
        action = EventType.RIGHT_CLICK_BLOCK
        block = object()
        block_face = object()
        is_cancelled = False

        def __init__(self, player):
            self.player = player

    plugin = Plugin()
    listener = namespace["SchematicToolListener"](plugin)
    player = LivePlayer("eight-events")
    events = [Event(player) for _ in range(8)]
    for event in events:
        listener.on_interact(event)
    assert plugin.forms.opens == 1
    assert all(event.is_cancelled for event in events)


def test_quit_releases_debounce_and_delegates_without_cancelling_jobs():
    namespace = _load_listener_namespace()
    player = Player("disconnect")
    calls = []

    class Plugin:
        @staticmethod
        def handle_player_quit(value):
            calls.append(value)

    claim = namespace["claim_interaction"]
    assert claim(player, "ninjos:schem_tablet", "menu", 0.45, now=30.0)

    listener = namespace["SchematicToolListener"](Plugin())
    listener.on_quit(types.SimpleNamespace(player=player))

    assert calls == [player]
    assert claim(player, "ninjos:schem_tablet", "menu", 0.45, now=30.01)
