import inspect
import sys
import types
from pathlib import Path


def test_listener_annotations_are_runtime_event_classes():
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
        listener = namespace["SchematicToolListener"](object())

        for name in ("on_interact", "on_quit"):
            signature = inspect.signature(getattr(listener, name))
            parameters = list(signature.parameters.values())
            assert len(parameters) == 1
            annotation = parameters[0].annotation
            assert inspect.isclass(annotation)
            assert issubclass(annotation, Event)
    finally:
        if old_endstone is None:
            sys.modules.pop("endstone", None)
        else:
            sys.modules["endstone"] = old_endstone
        if old_event is None:
            sys.modules.pop("endstone.event", None)
        else:
            sys.modules["endstone.event"] = old_event
