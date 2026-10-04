"""Regression cases for server-resolved block states and strict readback."""

import copy
import types

import pytest

from test_missing_blocks import Data, Dimension, _job, _plugin


class ResolvingServer:
    def __init__(self):
        self.calls = []

    def create_block_data(self, kind, states=None):
        states = dict(states or {})
        self.calls.append((kind, states))
        if kind == "minecraft:legacy_stairs":
            kind = "minecraft:oak_stairs"
        if kind == "minecraft:oak_stairs":
            states = {"weirdo_direction": 0, "upside_down_bit": False, **states}
        return Data(kind, states)


def stairs_job(count=1, states=None, kind="minecraft:oak_stairs"):
    job = _job(count)
    job.plan.palette[0] = {"type": kind, "states": dict(states or {})}
    return job


@pytest.mark.parametrize("states", [{}, {"weirdo_direction": 2}])
@pytest.mark.parametrize("kind", ["minecraft:oak_stairs", "minecraft:legacy_stairs"])
def test_server_resolved_defaults_and_aliases_verify_without_changing_saved_palette(states, kind):
    plugin = _plugin("abort")
    plugin.server = ResolvingServer()
    job = stairs_job(2, states, kind)
    original = copy.deepcopy(job.plan.palette)
    dimension = Dimension()

    assert plugin._paste_batch(job, dimension, 1) == 1
    assert plugin._paste_batch(job, dimension, 1) == 1

    assert job.placed == 2
    assert job.failed == job.state_fallbacks == 0
    assert plugin.server.calls == [(kind, states)]
    assert job.plan.palette == original
    for block in dimension.blocks.values():
        assert block.data.type.id == "minecraft:oak_stairs"
        assert block.data.block_states == {"weirdo_direction": 0, "upside_down_bit": False, **states}


def test_resolved_unchanged_block_skips_without_client_update():
    plugin = _plugin("abort")
    plugin.server = ResolvingServer()
    job = stairs_job()
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)
    block.data = Data("minecraft:oak_stairs", {"weirdo_direction": 0, "upside_down_bit": False})
    block.set_data = lambda *_args, **_kwargs: pytest.fail("unchanged resolved block was rewritten")

    assert plugin._paste_batch(job, dimension, 1) == 1
    assert job.skipped == 1
    assert job.placed == job.failed == job.write_attempts == 0


def test_retry_uses_the_same_resolved_data_and_preserves_undo():
    plugin = _plugin("abort")
    plugin.server = ResolvingServer()
    plugin._history_max_blocks_per_operation = 100
    job = stairs_job(states={"weirdo_direction": 2})
    job.capture_history = True
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)
    block.data = Data("minecraft:dirt")
    writes = []

    def write(data, **_kwargs):
        writes.append(data)
        if len(writes) == 2:
            block.data = data

    block.set_data = write
    assert plugin._paste_batch(job, dimension, 1) == 1
    assert job.placed == job.captured_blocks == 1
    assert len(writes) == 2 and writes[0] is writes[1]
    assert plugin.server.calls == [("minecraft:oak_stairs", {"weirdo_direction": 2})]
    assert job.before_palette == [{"type": "minecraft:dirt", "states": {}}]
    assert job.after_palette == [{"type": "minecraft:oak_stairs", "states": {"weirdo_direction": 2, "upside_down_bit": False}}]


def test_wrong_written_orientation_still_fails_and_saves_partial_undo():
    plugin = _plugin("abort")
    plugin.server = ResolvingServer()
    plugin._history_max_blocks_per_operation = 100
    job = stairs_job(states={"weirdo_direction": 2})
    job.capture_history = True
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)

    def wrong_state(_data, **_kwargs):
        block.data = Data("minecraft:oak_stairs", {"weirdo_direction": 0, "upside_down_bit": False})

    block.set_data = wrong_state
    with pytest.raises(RuntimeError, match="write verification failed"):
        plugin._paste_batch(job, dimension, 1)
    assert job.failed == job.captured_blocks == 1
    assert job.placed == 0


LIQUID_PAIRS = [
    ("minecraft:flowing_lava", "minecraft:lava"),
    ("minecraft:lava", "minecraft:flowing_lava"),
    ("minecraft:flowing_water", "minecraft:water"),
    ("minecraft:water", "minecraft:flowing_water"),
]


def liquid_job(kind, depth=0):
    return stairs_job(states={"liquid_depth": depth}, kind=kind)


@pytest.mark.parametrize("requested_type,written_type", LIQUID_PAIRS)
@pytest.mark.parametrize("depth", range(16))
def test_liquid_name_normalization_verifies_with_exact_depth(requested_type, written_type, depth):
    plugin = _plugin("abort")
    job = liquid_job(requested_type, depth)
    original = copy.deepcopy(job.plan.palette)
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)
    writes = []

    def write(data, **_kwargs):
        writes.append(data)
        block.data = Data(written_type, data.block_states)

    block.set_data = write
    assert plugin._paste_batch(job, dimension, 1) == 1
    assert job.placed == 1
    assert job.failed == job.state_fallbacks == 0
    assert len(writes) == 1
    assert block.data.block_states == {"liquid_depth": depth}
    assert job.plan.palette == original
    assert plugin._paste_integrity_error(job) is None


@pytest.mark.parametrize("requested_type,written_type", LIQUID_PAIRS)
@pytest.mark.parametrize("depth", [0, 7])
def test_equivalent_liquid_skips_without_writing_or_capturing_history(requested_type, written_type, depth):
    plugin = _plugin("abort")
    job = liquid_job(requested_type, depth)
    job.capture_history = True
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)
    block.data = Data(written_type, {"liquid_depth": depth})
    block.set_data = lambda *_args, **_kwargs: pytest.fail("equivalent liquid was rewritten")
    plugin._blockdata = types.SimpleNamespace(
        capture=lambda *_args: pytest.fail("unchanged liquid captured history")
    )

    assert plugin._paste_batch(job, dimension, 1) == 1
    assert job.skipped == 1
    assert job.placed == job.failed == job.write_attempts == job.captured_blocks == 0


@pytest.mark.parametrize("requested_type,written_type", LIQUID_PAIRS)
def test_liquid_retry_accepts_normalized_readback(requested_type, written_type):
    plugin = _plugin("abort")
    job = liquid_job(requested_type, 8)
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)
    writes = []

    def write(data, **_kwargs):
        writes.append(data)
        if len(writes) == 2:
            block.data = Data(written_type, data.block_states)

    block.set_data = write
    assert plugin._paste_batch(job, dimension, 1) == 1
    assert job.placed == 1 and job.failed == 0
    assert len(writes) == 2 and writes[0] is writes[1]


@pytest.mark.parametrize("requested_type,written_type", LIQUID_PAIRS[:1] + LIQUID_PAIRS[2:3])
@pytest.mark.parametrize("requested_depth,written_depth", [(0, 1), (7, 8), (8, 0)])
def test_wrong_liquid_depth_still_fails_and_preserves_partial_undo(
    requested_type, written_type, requested_depth, written_depth
):
    plugin = _plugin("abort")
    plugin._history_max_blocks_per_operation = 100
    job = liquid_job(requested_type, requested_depth)
    job.capture_history = True
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)

    def wrong_depth(_data, **_kwargs):
        block.data = Data(written_type, {"liquid_depth": written_depth})

    block.set_data = wrong_depth
    with pytest.raises(RuntimeError, match="write verification failed"):
        plugin._paste_batch(job, dimension, 1)
    assert job.failed == job.captured_blocks == 1
    assert job.placed == 0
    assert job.after_palette == [{"type": written_type, "states": {"liquid_depth": written_depth}}]


@pytest.mark.parametrize("requested_type,written_type", [
    ("minecraft:flowing_lava", "minecraft:water"),
    ("minecraft:flowing_water", "minecraft:lava"),
    ("minecraft:flowing_lava", "custom:lava"),
    ("minecraft:flowing_lava", "minecraft:air"),
])
def test_liquid_aliases_do_not_accept_wrong_material_or_ignored_write(requested_type, written_type):
    plugin = _plugin("abort")
    job = liquid_job(requested_type)
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)

    def wrong_type(_data, **_kwargs):
        if written_type != "minecraft:air":
            block.data = Data(written_type, {"liquid_depth": 0})

    block.set_data = wrong_type
    with pytest.raises(RuntimeError, match="write verification failed"):
        plugin._paste_batch(job, dimension, 1)
    assert job.failed == 1 and job.placed == 0


@pytest.mark.parametrize("requested_type,written_type", LIQUID_PAIRS)
def test_liquid_history_retains_actual_names_for_undo_and_redo(requested_type, written_type):
    plugin = _plugin("abort")
    plugin._history_max_blocks_per_operation = 100
    plugin._tick_counter = 0
    job = liquid_job(requested_type)
    job.capture_history = True
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)
    block.data = Data("minecraft:dirt")

    def write(data, **_kwargs):
        kind = written_type if data.type.id == requested_type else data.type.id
        block.data = Data(kind, data.block_states)

    block.set_data = write
    assert plugin._paste_batch(job, dimension, 1) == 1
    assert job.before_palette == [{"type": "minecraft:dirt", "states": {}}]
    assert job.after_palette == [{"type": written_type, "states": {"liquid_depth": 0}}]
    history = plugin._history_entry_from_job(job)

    undo = liquid_job(requested_type)
    undo.plan = history.before_plan
    undo.operation = "undo"
    assert plugin._paste_batch(undo, dimension, 1) == 1
    assert undo.failed == 0 and block.data.type.id == "minecraft:dirt"

    redo = liquid_job(requested_type)
    redo.plan = history.after_plan
    redo.operation = "redo"
    assert plugin._paste_batch(redo, dimension, 1) == 1
    assert redo.failed == 0 and block.data.type.id == written_type
    assert block.data.block_states == {"liquid_depth": 0}


def test_failure_details_fit_chat_messages_and_keep_full_console_error():
    plugin = _plugin("abort")
    job = stairs_job()
    plugin.paste_jobs = {job.player_uuid: job}
    messages, logs = [], []
    plugin.server.get_player = lambda _uuid: types.SimpleNamespace(send_error_message=messages.append)
    plugin.logger = types.SimpleNamespace(error=logs.append)
    plugin._release_job_chunk = lambda *_args, **_kwargs: None
    reason = (
        "paste verification stopped after 1 failure(s): write verification failed at 1, 90, 0: "
        "expected minecraft:oak_stairs {'minecraft:corner': 'none', 'weirdo_direction': 2, "
        "'upside_down_bit': False}, got minecraft:air {}"
    )
    plugin._fail_paste_job(job, reason)
    assert messages[0] == "Schematic paste failed:"
    assert all(len(line) <= 180 for line in messages)
    assert "got minecraft:air {}" in " ".join(messages)
    assert reason in logs[0]
    assert job.player_uuid not in plugin.paste_jobs
