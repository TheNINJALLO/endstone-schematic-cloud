import importlib
import sys
import types

from endstone_ninjos_schematics.models import BlockPos, PasteChunkRange, PasteJob, PastePlan


def _load_plugin_module():
    fake_endstone = types.ModuleType("endstone")
    fake_endstone.Player = type("Player", (), {})

    fake_command = types.ModuleType("endstone.command")
    fake_command.Command = type("Command", (), {})
    fake_command.CommandSender = type("CommandSender", (), {})

    fake_inventory = types.ModuleType("endstone.inventory")
    fake_inventory.ItemStack = type("ItemStack", (), {})

    fake_plugin = types.ModuleType("endstone.plugin")
    fake_plugin.Plugin = type("Plugin", (), {})

    fake_form = types.ModuleType("endstone.form")
    for name in ("ActionForm", "ModalForm", "TextInput", "Toggle"):
        setattr(fake_form, name, type(name, (), {}))

    fake_event = types.ModuleType("endstone.event")
    fake_event.PlayerInteractEvent = type(
        "PlayerInteractEvent",
        (),
        {
            "LEFT_CLICK_BLOCK": 0,
            "RIGHT_CLICK_BLOCK": 1,
            "LEFT_CLICK_AIR": 2,
            "RIGHT_CLICK_AIR": 3,
        },
    )
    fake_event.PlayerQuitEvent = type("PlayerQuitEvent", (), {})
    fake_event.event_handler = lambda func: func

    names = {
        "endstone": fake_endstone,
        "endstone.command": fake_command,
        "endstone.inventory": fake_inventory,
        "endstone.plugin": fake_plugin,
        "endstone.form": fake_form,
        "endstone.event": fake_event,
    }
    old = {name: sys.modules.get(name) for name in names}
    sys.modules.update(names)
    try:
        sys.modules.pop("endstone_ninjos_schematics.plugin", None)
        return importlib.import_module("endstone_ninjos_schematics.plugin")
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_history_capture_builds_matching_chunk_sorted_plans():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._history_max_blocks_per_operation = 100
    plugin._history_max_operations = 5
    plugin._history_max_total_blocks = 100
    plugin._tick_counter = 25
    plugin.undo_history = {}
    plugin.redo_history = {}

    empty = PastePlan(
        size=(18, 1, 1),
        palette=[],
        records=b"",
        chunks=(PasteChunkRange(0, 0, 0, 0),),
    )
    job = PasteJob(
        player_uuid="p",
        name="bridge",
        plan=empty,
        dimension_id="Overworld",
        anchor=BlockPos(15, 64, 0),
        rotation=0,
        capture_history=True,
    )
    plugin._capture_history_change(
        job, 0, 0, 0, "minecraft:dirt", {}, "minecraft:stone", {}
    )
    plugin._capture_history_change(
        job, 1, 0, 0, "minecraft:grass_block", {}, "minecraft:stone", {}
    )

    entry = plugin._history_entry_from_job(job)
    assert entry is not None
    assert entry.block_count == 2
    assert entry.before_plan.block_count == 2
    assert entry.after_plan.block_count == 2
    assert [(c.chunk_x, c.start, c.end) for c in entry.before_plan.chunks] == [
        (0, 0, 1),
        (1, 1, 2),
    ]
    plugin._push_undo_history("p", entry, clear_redo=True)
    assert plugin.undo_history["p"][-1] is entry


def test_paste_undo_redo_round_trip_restores_exact_states():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._history_max_blocks_per_operation = 100
    plugin._history_max_operations = 5
    plugin._history_max_total_blocks = 100
    plugin._history_enabled = True
    plugin._skip_unchanged = True
    plugin._apply_physics = False
    plugin._paste_budget = 100
    plugin._tick_counter = 10
    plugin.undo_history = {}
    plugin.redo_history = {}
    plugin.paste_jobs = {}
    plugin.save_jobs = {}
    plugin.preparing_pastes = {}

    class Type:
        def __init__(self, identifier):
            self.id = identifier

    class Data:
        def __init__(self, identifier, states=None):
            self.type = Type(identifier)
            self.block_states = dict(states or {})

    class Block:
        def __init__(self, data):
            self.data = data

        def set_data(self, data, apply_physics=False):
            self.data = data

        def set_type(self, identifier, apply_physics=False):
            self.data = Data(identifier)

    class Dimension:
        def __init__(self):
            self.blocks = {
                (0, 64, 0): Block(Data("minecraft:dirt", {"snowy": False})),
                (1, 64, 0): Block(Data("minecraft:oak_log", {"pillar_axis": "y"})),
            }

        def get_block_at(self, x, y, z):
            return self.blocks[(x, y, z)]

    class Player:
        unique_id = "player"
        is_op = True

        def has_permission(self, _permission):
            return True

        def send_message(self, _message):
            pass

        def send_error_message(self, message):
            raise AssertionError(message)

    dimension = Dimension()
    player = Player()

    class Server:
        def create_block_data(self, identifier, states):
            return Data(identifier, states)

        def get_player(self, uuid):
            return player if uuid == player.unique_id else None

    plugin.server = Server()

    records = bytearray()
    from endstone_ninjos_schematics.codec import append_record

    append_record(records, 0, 0, 0, 0)
    append_record(records, 1, 0, 0, 1)
    plan = PastePlan(
        size=(2, 1, 1),
        palette=[
            {"type": "minecraft:stone", "states": {}},
            {"type": "minecraft:oak_log", "states": {"pillar_axis": "x"}},
        ],
        records=bytes(records),
        chunks=(PasteChunkRange(0, 0, 0, 2),),
    )
    paste = PasteJob(
        player_uuid=player.unique_id,
        name="round-trip",
        plan=plan,
        dimension_id="Overworld",
        anchor=BlockPos(0, 64, 0),
        rotation=0,
        operation="paste",
        capture_history=True,
    )
    plugin.paste_jobs[player.unique_id] = paste
    assert plugin._paste_batch(paste, dimension, 2) == 2
    plugin._complete_paste_job(paste)
    assert dimension.blocks[(0, 64, 0)].data.type.id == "minecraft:stone"
    assert dimension.blocks[(1, 64, 0)].data.block_states["pillar_axis"] == "x"
    assert len(plugin.undo_history[player.unique_id]) == 1

    plugin.start_history_action(player, "undo")
    undo = plugin.paste_jobs[player.unique_id]
    plugin._paste_batch(undo, dimension, undo.plan.block_count)
    plugin._complete_paste_job(undo)
    assert dimension.blocks[(0, 64, 0)].data.type.id == "minecraft:dirt"
    assert dimension.blocks[(0, 64, 0)].data.block_states == {"snowy": False}
    assert dimension.blocks[(1, 64, 0)].data.block_states == {"pillar_axis": "y"}
    assert len(plugin.redo_history[player.unique_id]) == 1

    plugin.start_history_action(player, "redo")
    redo = plugin.paste_jobs[player.unique_id]
    plugin._paste_batch(redo, dimension, redo.plan.block_count)
    plugin._complete_paste_job(redo)
    assert dimension.blocks[(0, 64, 0)].data.type.id == "minecraft:stone"
    assert dimension.blocks[(1, 64, 0)].data.block_states == {"pillar_axis": "x"}


def test_paste_batch_yields_after_wall_clock_deadline():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._max_paste_failures = 10
    plugin._verify_paste_writes = True
    module.monotonic = lambda: 2.0

    records = bytearray()
    from endstone_ninjos_schematics.codec import append_record

    for dx in range(3):
        append_record(records, dx, 0, 0, 0)
    plan = PastePlan(
        size=(3, 1, 1),
        palette=[],
        records=bytes(records),
        chunks=(PasteChunkRange(0, 0, 0, 3),),
    )
    job = PasteJob(
        player_uuid="player",
        name="time-budget",
        plan=plan,
        dimension_id="Overworld",
        anchor=BlockPos(0, 64, 0),
        rotation=0,
    )

    assert plugin._paste_batch(job, object(), 3, deadline=1.0) == 1
    assert job.cursor == 1


def test_player_quit_keeps_active_paste_but_releases_interactive_state():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    active = types.SimpleNamespace(name="large-build", operation="paste")
    plugin.paste_jobs = {"player": active}
    plugin.save_jobs = {}
    plugin.placements = {"player": "preview"}
    plugin.preparing_pastes = {"player": (object(), "planning")}
    cleaned = []
    plugin._cleanup_placement = cleaned.append
    messages = []
    plugin.logger = types.SimpleNamespace(info=messages.append)
    player = types.SimpleNamespace(unique_id="player")

    plugin.handle_player_quit(player)

    assert plugin.paste_jobs["player"] is active
    assert "player" not in plugin.placements
    assert "player" not in plugin.preparing_pastes
    assert cleaned == ["preview", "planning"]
    assert any("continuing active schematic paste" in message for message in messages)


def test_paste_readback_stops_silent_failed_write():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._history_max_blocks_per_operation = 100
    plugin._skip_unchanged = True
    plugin._apply_physics = False
    plugin._verify_paste_writes = True
    plugin._max_paste_failures = 0

    class Type:
        def __init__(self, identifier):
            self.id = identifier

    class Data:
        def __init__(self, identifier, states=None):
            self.type = Type(identifier)
            self.block_states = dict(states or {})

    class IgnoringBlock:
        def __init__(self):
            self.data = Data("minecraft:dirt")

        def set_data(self, _data, apply_physics=False):
            pass

        def set_type(self, _identifier, apply_physics=False):
            pass

    block = IgnoringBlock()

    class Dimension:
        def get_block_at(self, _x, _y, _z):
            return block

    class Server:
        def create_block_data(self, identifier, states):
            return Data(identifier, states)

    plugin.server = Server()
    records = bytearray()
    from endstone_ninjos_schematics.codec import append_record

    append_record(records, 0, 0, 0, 0)
    plan = PastePlan(
        size=(1, 1, 1),
        palette=[{"type": "minecraft:stone", "states": {}}],
        records=bytes(records),
        chunks=(PasteChunkRange(0, 0, 0, 1),),
    )
    job = PasteJob(
        player_uuid="player",
        name="verify",
        plan=plan,
        dimension_id="Overworld",
        anchor=BlockPos(0, 64, 0),
        rotation=0,
    )
    try:
        plugin._paste_batch(job, Dimension(), 1)
    except RuntimeError as exc:
        assert "write verification failed" in str(exc)
    else:
        raise AssertionError("a silently ignored block write was accepted")
    assert job.failed == 1
