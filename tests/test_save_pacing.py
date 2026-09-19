from types import SimpleNamespace

import pytest

from endstone_ninjos_schematics.codec import iter_records
from endstone_ninjos_schematics.models import BlockPos, SaveJob
from endstone_ninjos_schematics.planner import build_chunk_regions
from test_missing_blocks import Data, _plugin


def save_job(player="builder", size=(16, 2, 16)):
    low = BlockPos(-19, 64, -3)
    return SaveJob(
        player, player, "", "build", "Build", "", False, True, "Overworld",
        low, size, size[0] * size[1] * size[2], build_chunk_regions(low, size),
    )


def setup(plugin, jobs, world):
    plugin._scan_budget = 2500
    plugin._tick_counter = 0
    plugin._progress_interval = 99999
    plugin._auto_load_chunks = True
    plugin._chunk_load_timeout = 40
    plugin._chunk_stabilize_ticks = 2
    plugin._max_chunk_retries = 3
    plugin._legacy_ticket_slots = {}
    plugin.server.get_player = lambda _uuid: None
    plugin._get_dimension = lambda _id: world
    plugin.save_jobs = {job.player_uuid: job for job in jobs}
    plugin.logger = SimpleNamespace(debug=lambda *_args: None)

    def fail(job, reason):
        pytest.fail(reason)

    plugin._fail_save_job = fail


def test_scan_yields_after_slow_read_and_rotates_players(monkeypatch):
    plugin = _plugin("skip")
    jobs = [save_job("a"), save_job("b")]
    clock = [0.0]

    def read(*_args):
        clock[0] += 0.020
        return SimpleNamespace(data=Data("minecraft:stone"))

    world = SimpleNamespace(get_block_at=read)
    setup(plugin, jobs, world)
    plugin._ensure_job_chunk = lambda *_args: True
    plugin._job_chunk_is_resident = lambda *_args: True
    monkeypatch.setitem(plugin._process_save_jobs.__func__.__globals__, "monotonic", lambda: clock[0])
    plugin._process_save_jobs()
    assert [job.cursor for job in jobs] == [1, 0]
    plugin._process_save_jobs()
    assert [job.cursor for job in jobs] == [1, 1]


def test_metadata_capture_batches_are_small_and_resume_without_gaps(monkeypatch):
    plugin = _plugin("skip")
    job = save_job(size=(16, 20, 16))
    clock = [0.0]
    captures = []

    def capture(_dimension, low, high):
        volume = (high[0] - low[0] + 1) * (high[1] - low[1] + 1) * (high[2] - low[2] + 1)
        assert volume <= 512
        captures.append(volume)
        clock[0] += 0.020
        return {}

    plugin._blockdata = SimpleNamespace(capture_region=capture)
    world = SimpleNamespace(get_block_at=lambda *_args: SimpleNamespace(data=Data("minecraft:stone")))
    monkeypatch.setitem(plugin._scan_save_batch.__func__.__globals__, "monotonic", lambda: clock[0])
    for _ in range(3):
        assert plugin._scan_save_batch(job, world, 2500, deadline=clock[0] + 0.005) == 1
    assert job.cursor == 3
    assert len(captures) == 3
    assert [(x, y, z) for x, y, z, _ in iter_records(job.records)] == [(0, 0, 0), (1, 0, 0), (2, 0, 0)]


@pytest.mark.parametrize("include_air", [True, False])
def test_full_save_waits_for_unloaded_chunks_and_survives_disconnect(monkeypatch, include_air):
    plugin = _plugin("skip")
    job = save_job(size=(35, 3, 34))
    job.include_air = include_air
    holds = {}
    reads = []
    released = []
    completed = []

    class World:
        def load_chunk(self, x, z):
            holds[x, z] = plugin._tick_counter + 3
            return True

        def is_chunk_loaded(self, x, z):
            return (x, z) in holds and plugin._tick_counter >= holds[x, z]

        def unload_chunk_request(self, x, z):
            released.append((x, z))
            holds.pop((x, z))

        def get_block_at(self, x, y, z):
            assert self.is_chunk_loaded(x // 16, z // 16)
            assert plugin._tick_counter >= holds[x // 16, z // 16] + 2
            reads.append((x, y, z))
            return SimpleNamespace(data=Data("minecraft:stone" if (x + y + z) % 3 else "minecraft:air"))

    setup(plugin, [job], World())
    plugin._finish_save_scan = lambda j: completed.append(plugin._validate_save_integrity(j))
    monkeypatch.setitem(plugin._process_save_jobs.__func__.__globals__, "monotonic", lambda: 0)
    for tick in range(200):
        plugin._tick_counter = tick
        plugin._process_save_jobs()
        if not plugin.save_jobs:
            break
    assert completed
    assert job.cursor == job.total_volume == len(reads) == len(set(reads))
    expected = {p for p in reads if include_air or sum(p) % 3}
    actual = {(job.low.x + x, job.low.y + y, job.low.z + z) for x, y, z, _ in iter_records(job.records)}
    assert actual == expected
    assert len(released) == len(job.regions)
    assert not holds and not plugin._native_chunk_holds


def test_save_restarts_whole_region_if_chunk_drops_between_ticks():
    plugin = _plugin("skip")
    job = save_job()
    loaded = [True]
    world = SimpleNamespace(
        is_chunk_loaded=lambda *_args: loaded[0], load_chunk=lambda *_args: True,
        get_block_at=lambda *_args: SimpleNamespace(data=Data("minecraft:stone")),
    )
    setup(plugin, [job], world)
    assert plugin._ensure_job_chunk(job, world, -2, -1)
    plugin._begin_save_region_snapshot(job)
    plugin._scan_save_batch(job, world, 2)
    assert job.cursor == 2
    plugin._tick_counter = 1
    loaded[0] = False
    assert not plugin._ensure_job_chunk(job, world, -2, -1)
    assert job.cursor == job.region_cursor == len(job.records) == 0
    assert job.chunk_retries == 1
    loaded[0] = True
    plugin._tick_counter = 2
    assert not plugin._ensure_job_chunk(job, world, -2, -1)
    plugin._tick_counter = 4
    assert plugin._ensure_job_chunk(job, world, -2, -1)
