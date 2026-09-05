import importlib
import sys
import types

from endstone_ninjos_schematics.chunk_loading import (
    chunk_block_bounds,
    chunk_loaded_state,
    command_dimension_name,
    ticket_name,
    tickingarea_add_command,
)
from endstone_ninjos_schematics.models import BlockPos, SaveJob
from endstone_ninjos_schematics.planner import build_chunk_regions


class Chunk:
    def __init__(self, x, z):
        self.x = x
        self.z = z


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


def _job():
    low = BlockPos(-17, 0, 31)
    size = (1, 1, 1)
    return SaveJob(
        player_uuid="player",
        player_name="Architect",
        player_xuid="",
        name="test",
        display_name="test",
        description="",
        overwrite=False,
        include_air=True,
        dimension_id="Overworld",
        low=low,
        size=size,
        total_volume=1,
        regions=build_chunk_regions(low, size),
    )


def test_chunk_helpers_support_legacy_loaded_chunks_and_negative_coordinates():
    dimension = type("LegacyDimension", (), {"loaded_chunks": [Chunk(-2, 1)]})()
    assert chunk_loaded_state(dimension, -2, 1) is True
    assert chunk_loaded_state(dimension, -1, 1) is False
    assert chunk_block_bounds(-2, 1) == (-32, 16, -17, 31)
    assert command_dimension_name("minecraft:the_end") == "the_end"


def test_tickingarea_command_is_dimension_scoped_and_preloaded():
    name = ticket_name("NJS Schem", 3)
    command = tickingarea_add_command("Nether", -2, 1, name, preload=True)
    assert command.startswith("execute in nether run tickingarea add -32 0 16 -17 0 31 ")
    assert command.endswith(f"{name} true")


def test_legacy_runtime_holds_even_already_loaded_chunk_before_scanning():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._auto_load_chunks = True
    plugin._legacy_tickingarea_fallback = True
    plugin._legacy_tickingarea_preload = True
    plugin._legacy_tickingarea_prefix = "njs_schem"
    plugin._legacy_tickingarea_max_active = 8
    plugin._legacy_ticket_slots = {}
    plugin._chunk_load_timeout = 100
    plugin._chunk_stabilize_ticks = 2
    plugin._tick_counter = 0

    commands = []

    class Server:
        command_sender = object()

        @staticmethod
        def dispatch_command(_sender, command):
            commands.append(command)
            return True

    plugin.server = Server()
    plugin.logger = type("Logger", (), {"debug": lambda *args: None})()
    dimension = type("LegacyDimension", (), {"loaded_chunks": [Chunk(-2, 1)]})()
    job = _job()

    # Even though the chunk is already visible in loaded_chunks, the first call acquires
    # a plugin-owned ticking area instead of trusting player proximity.
    assert plugin._ensure_job_chunk(job, dimension, -2, 1) is False
    assert job.ticket_backend == "tickingarea"
    assert any("tickingarea add" in command for command in commands)

    plugin._tick_counter = 1
    assert plugin._ensure_job_chunk(job, dimension, -2, 1) is False
    plugin._tick_counter = 3
    assert plugin._ensure_job_chunk(job, dimension, -2, 1) is True

    plugin._release_job_chunk(job, dimension, release_slot=True)
    assert any("tickingarea remove" in command for command in commands)
    assert plugin._legacy_ticket_slots == {}


def test_legacy_slot_is_reused_between_chunks_and_released_at_job_end():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._auto_load_chunks = True
    plugin._legacy_tickingarea_fallback = True
    plugin._legacy_tickingarea_preload = True
    plugin._legacy_tickingarea_prefix = "njs_schem"
    plugin._legacy_tickingarea_max_active = 8
    plugin._legacy_ticket_slots = {}
    plugin._legacy_ticket_registry = {}
    plugin._legacy_ticket_session = "abc123"
    plugin._chunk_load_timeout = 100
    plugin._chunk_stabilize_ticks = 0
    plugin._tick_counter = 0

    commands = []

    class Server:
        command_sender = object()

        @staticmethod
        def dispatch_command(_sender, command):
            commands.append(command)
            return True

    plugin.server = Server()
    plugin.logger = type("Logger", (), {"debug": lambda *args: None})()
    dimension = type("LegacyDimension", (), {"loaded_chunks": [Chunk(-2, 1), Chunk(-1, 1)]})()
    job = _job()

    assert plugin._ensure_job_chunk(job, dimension, -2, 1) is False
    first_name = job.ticket_name
    first_slot = job.ticket_slot
    plugin._release_job_chunk(job, dimension)
    assert job.ticket_name == first_name
    assert job.ticket_slot == first_slot
    assert plugin._legacy_ticket_slots[first_slot] is job

    plugin._tick_counter = 1
    assert plugin._ensure_job_chunk(job, dimension, -1, 1) is False
    assert job.ticket_name == first_name
    assert job.ticket_slot == first_slot

    plugin._release_job_chunk(job, dimension, release_slot=True)
    assert plugin._legacy_ticket_slots == {}
    assert job.ticket_name is None
    assert job.ticket_slot is None


def test_modern_runtime_registers_direct_ticket_even_when_chunk_is_loaded():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._auto_load_chunks = True
    plugin._legacy_tickingarea_fallback = True
    plugin._legacy_ticket_slots = {}
    plugin._chunk_load_timeout = 100
    plugin._chunk_stabilize_ticks = 0
    plugin._tick_counter = 0
    plugin.logger = type("Logger", (), {"debug": lambda *args: None})()

    class Dimension:
        def __init__(self):
            self.loads = []
            self.unloads = []

        def is_chunk_loaded(self, x, z):
            return True

        def load_chunk(self, x, z):
            self.loads.append((x, z))
            return True

        def unload_chunk(self, x, z):
            self.unloads.append((x, z))
            return True

    dimension = Dimension()
    job = _job()
    assert plugin._ensure_job_chunk(job, dimension, -2, 1) is False
    assert dimension.loads == [(-2, 1)]
    plugin._tick_counter = 1
    assert plugin._ensure_job_chunk(job, dimension, -2, 1) is True
    plugin._release_job_chunk(job, dimension)
    assert dimension.unloads == [(-2, 1)]


def test_modern_runtime_prefers_deferred_chunk_release():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin._legacy_ticket_slots = {}
    plugin.logger = type("Logger", (), {"debug": lambda *args: None})()
    job = _job()
    job.ticket_chunk = (-2, 1)
    job.ticket_owned = True
    job.ticket_backend = "endstone"

    class Dimension:
        def __init__(self):
            self.requests = []
            self.synchronous_unloads = []

        def unload_chunk_request(self, x, z):
            self.requests.append((x, z))
            return True

        def unload_chunk(self, x, z):
            self.synchronous_unloads.append((x, z))
            return True

    dimension = Dimension()
    plugin._release_job_chunk(job, dimension)
    assert dimension.requests == [(-2, 1)]
    assert dimension.synchronous_unloads == []


def test_save_integrity_requires_verified_chunk_and_full_air_coverage():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    job = _job()
    from endstone_ninjos_schematics.codec import append_record

    append_record(job.records, 0, 0, 0, 0)
    job.palette = [{"type": "minecraft:air", "states": {}}]
    job.palette_lookup = {"air": 0}
    job.cursor = 1
    job.region_cursor = 1
    job.verified_regions = 1
    stored, volume = plugin._validate_save_integrity(job)
    assert stored == 1
    assert volume == 1

    job.verified_regions = 0
    try:
        plugin._validate_save_integrity(job)
    except RuntimeError as exc:
        assert "verified 0 of 1" in str(exc)
    else:
        raise AssertionError("unverified save unexpectedly passed")


def test_startup_cleanup_only_targets_journaled_ticking_areas(tmp_path):
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin.data_folder = tmp_path
    plugin._legacy_tickingarea_fallback = True
    plugin._legacy_ticket_registry = {}
    plugin._stale_legacy_ticket_records = []

    messages = []
    plugin.logger = type(
        "Logger",
        (),
        {
            "debug": lambda *args: None,
            "warning": lambda *args: None,
            "info": lambda _self, message: messages.append(message),
        },
    )()
    commands = []
    plugin._dispatch_console_command = lambda command: commands.append(command) or True

    # Empty journal means no speculative 8-slots-by-3-dimensions cleanup storm.
    plugin._cleanup_stale_legacy_tickets()
    assert commands == []

    plugin._stale_legacy_ticket_records = [
        {
            "name": "njs_schem_deadbeef_0",
            "dimension_id": "minecraft:overworld",
            "chunk_x": 0,
            "chunk_z": 0,
        }
    ]
    plugin._cleanup_stale_legacy_tickets()
    assert commands == [
        "execute in overworld run tickingarea remove njs_schem_deadbeef_0"
    ]
    assert any("1 journaled area" in message for message in messages)


def test_legacy_ticket_journal_round_trip(tmp_path):
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin.data_folder = tmp_path
    plugin._legacy_ticket_registry = {}
    plugin.logger = type(
        "Logger",
        (),
        {"warning": lambda *args: None, "debug": lambda *args: None},
    )()

    plugin._remember_legacy_ticket(
        "njs_schem_session_0", "minecraft:nether", -12, 7
    )
    loaded = plugin._load_legacy_ticket_registry()
    assert loaded == [
        {
            "name": "njs_schem_session_0",
            "dimension_id": "minecraft:nether",
            "chunk_x": -12,
            "chunk_z": 7,
        }
    ]

    plugin._forget_legacy_ticket("njs_schem_session_0")
    assert plugin._load_legacy_ticket_registry() == []
