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
