import importlib
import sys
import types

import pytest

from endstone_ninjos_schematics.codec import append_record
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


class Type:
    def __init__(self, identifier):
        self.id = identifier


class Data:
    def __init__(self, identifier, states=None):
        self.type = Type(identifier)
        self.block_states = dict(states or {})


class Block:
    def __init__(self):
        self.data = Data("minecraft:air")

    def set_data(self, data, apply_physics=False):
        self.data = data

    def set_type(self, identifier, apply_physics=False):
        if identifier.startswith("medieval:"):
            raise RuntimeError(f"Block type {identifier} cannot be found in the registry")
        self.data = Data(identifier)


class Dimension:
    def __init__(self):
        self.blocks = {}

    def get_block_at(self, x, y, z):
        return self.blocks.setdefault((x, y, z), Block())


class Server:
    def __init__(self):
        self.calls = []

    def create_block_data(self, identifier, states=None):
        self.calls.append((identifier, dict(states or {})))
        if identifier.startswith("medieval:"):
            raise RuntimeError(f"Block type {identifier} cannot be found in the registry")
        return Data(identifier, states)


def _plugin(policy):
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin.server = Server()
    plugin._skip_unchanged = True
    plugin._apply_physics = False
    plugin._verify_paste_writes = True
    plugin._max_paste_failures = 0
    plugin._missing_block_policy = policy
    plugin._missing_block_fallback = "minecraft:stone"
    plugin._missing_block_report_limit = 20
    return plugin


def _job(count=1):
    records = bytearray()
    for x in range(count):
        append_record(records, x, 0, 0, 0)
    plan = PastePlan(
        size=(count, 1, 1),
        palette=[{"type": "medieval:deepslate_tile_wall_0", "states": {}}],
        records=bytes(records),
        chunks=(PasteChunkRange(0, 0, 0, count),),
    )
    return PasteJob(
        player_uuid="player",
        name="custom-build",
        plan=plan,
        dimension_id="Overworld",
        anchor=BlockPos(0, 64, 0),
        rotation=0,
    )


def test_missing_custom_block_skip_continues_and_caches_registry_failure():
    plugin = _plugin("skip")
    dimension = Dimension()
    job = _job(2)

    assert plugin._paste_batch(job, dimension, 2) == 2
    assert job.failed == 0
    assert job.skipped == 2
    assert job.missing_blocks == 2
    assert job.missing_substitutions == 0
    assert job.palette_modes == {0: "missing"}
    assert job.missing_type_counts == {"medieval:deepslate_tile_wall_0": 2}
    # Exact-state and type-only probes happen once, not once per block record.
    assert len(plugin.server.calls) == 2
    assert plugin._paste_integrity_error(job) is None


def test_missing_custom_block_can_be_substituted_with_configured_fallback():
    plugin = _plugin("fallback")
    dimension = Dimension()
    job = _job(1)

    assert plugin._paste_batch(job, dimension, 1) == 1
    assert job.failed == 0
    assert job.placed == 1
    assert job.missing_blocks == 1
    assert job.missing_substitutions == 1
    assert dimension.get_block_at(0, 64, 0).data.type.id == "minecraft:stone"
    assert plugin._paste_integrity_error(job) is None


def test_missing_custom_block_abort_retains_strict_mode():
    plugin = _plugin("abort")
    dimension = Dimension()
    job = _job(1)

    with pytest.raises(RuntimeError, match="unavailable in the target server registry"):
        plugin._paste_batch(job, dimension, 1)
    assert job.failed == 1
    assert job.missing_blocks == 1
