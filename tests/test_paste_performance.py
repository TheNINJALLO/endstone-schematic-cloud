import types

from test_missing_blocks import Dimension, _job, _plugin


def stone_job(player="player", count=12):
    job = _job(count)
    job.player_uuid = player
    job.plan.palette[0] = {"type": "minecraft:stone", "states": {}}
    return job


def test_palette_data_is_reused_across_batches():
    plugin = _plugin("skip")
    job = stone_job()
    dimension = Dimension()
    assert plugin._paste_batch(job, dimension, 6) == 6
    assert plugin._paste_batch(job, dimension, 6) == 6
    assert job.placed == 12
    assert plugin.server.calls == [("minecraft:stone", {})]


def test_unchanged_air_skips_blockdata_capture_and_change_budget():
    plugin = _plugin("skip")
    job = stone_job()
    job.plan.palette[0] = {"type": "minecraft:air", "states": {}}
    job.capture_history = True

    def unexpected_capture(*args):
        raise AssertionError("unchanged air should not capture undo metadata")

    plugin._blockdata = types.SimpleNamespace(capture=unexpected_capture)
    assert plugin._paste_batch(job, Dimension(), 12, max_changes=1) == 12
    assert job.skipped == 12
    assert job.write_attempts == job.captured_blocks == 0


def test_change_limit_resumes_without_skipping_records():
    plugin = _plugin("skip")
    job = stone_job()
    dimension = Dimension()
    for expected_cursor in (3, 6, 9, 12):
        assert plugin._paste_batch(job, dimension, 12, max_changes=3) == 3
        assert job.cursor == job.placed == expected_cursor
    assert job.write_attempts == 12
    assert all(block.data.type.id == "minecraft:stone" for block in dimension.blocks.values())


def scheduler(plugin, jobs, dimension, monkeypatch):
    plugin._paste_budget = 1200
    plugin._paste_change_budget = 4
    plugin._tick_counter = 0
    plugin._progress_interval = 40
    plugin.paste_jobs = {job.player_uuid: job for job in jobs}
    plugin.server.get_player = lambda _uuid: None
    plugin._get_dimension = lambda _dimension: dimension
    plugin._ensure_job_chunk = lambda *_args: True
    plugin._job_chunk_is_resident = lambda *_args: True
    monkeypatch.setitem(plugin._process_paste_jobs.__func__.__globals__, "monotonic", lambda: 0)


def test_concurrent_pastes_share_change_limit(monkeypatch):
    plugin = _plugin("skip")
    jobs = [stone_job("a"), stone_job("b")]
    dimension = Dimension()
    jobs[1].anchor = types.SimpleNamespace(x=16, y=64, z=0)
    scheduler(plugin, jobs, dimension, monkeypatch)
    for _ in range(3):
        plugin._process_paste_jobs()
    assert [job.write_attempts for job in jobs] == [6, 6]


def test_failed_paste_still_consumes_shared_change_limit(monkeypatch):
    plugin = _plugin("skip")
    jobs = [stone_job("a"), stone_job("b")]
    dimension = Dimension()
    scheduler(plugin, jobs, dimension, monkeypatch)
    plugin._paste_change_budget = 1
    dimension.get_block_at(0, 64, 0).set_data = lambda *_args, **_kwargs: None
    plugin._fail_paste_job = lambda job, _reason: plugin.paste_jobs.pop(job.player_uuid)
    plugin._process_paste_jobs()
    assert jobs[0].failed == 1
    assert jobs[0].write_attempts == 1
    assert jobs[1].cursor == 0


def test_slow_job_does_not_starve_next_player(monkeypatch):
    plugin = _plugin("skip")
    jobs = [stone_job("a"), stone_job("b")]
    dimension = Dimension()
    scheduler(plugin, jobs, dimension, monkeypatch)
    clock = [0.0]
    monkeypatch.setitem(plugin._process_paste_jobs.__func__.__globals__, "monotonic", lambda: clock[0])

    def expensive_write(data, **_kwargs):
        block.data = data
        clock[0] += 1.0

    block = dimension.get_block_at(0, 64, 0)
    block.set_data = expensive_write
    plugin._process_paste_jobs()
    assert jobs[0].cursor == 1
    assert jobs[1].cursor == 0
    plugin._process_paste_jobs()
    assert jobs[1].cursor > 0


def test_set_data_fallback_invalidates_exact_palette_cache():
    plugin = _plugin("skip")
    job = stone_job(count=2)
    job.plan.palette[0]["states"] = {"unsupported": True}
    dimension = Dimension()
    block = dimension.get_block_at(0, 64, 0)

    def rejected_states(*_args, **_kwargs):
        raise ValueError("unsupported state")

    block.set_data = rejected_states
    assert plugin._paste_batch(job, dimension, 2) == 2
    assert job.state_fallbacks == 2
    assert dimension.get_block_at(1, 64, 0).data.block_states == {}
    assert plugin.server.calls == [("minecraft:stone", {"unsupported": True}), ("minecraft:stone", {})]


def test_streamed_paste_finishes_in_initially_unloaded_world(tmp_path, monkeypatch):
    from endstone_ninjos_schematics.codec import RECORD, append_record, iter_records
    from endstone_ninjos_schematics.models import BlockPos, DecodedSchematic, PasteJob
    from endstone_ninjos_schematics.planner import prepare_streaming_paste_plan
    from endstone_ninjos_schematics.record_store import SpillRecordBuffer

    records = bytearray()
    for y in range(8):
        for z in range(24):
            for x in range(24):
                append_record(records, x, y, z, 0)
    schematic = DecodedSchematic(
        {"size": [24, 8, 24], "block_count": len(records) // RECORD.size},
        [{"type": "minecraft:stone", "states": {}}], records,
    )
    anchor = BlockPos(7, 64, -11)
    plan = prepare_streaming_paste_plan(
        schematic, anchor, 90,
        lambda prefix: SpillRecordBuffer(tmp_path, threshold_bytes=64, prefix=prefix),
        batch_records=1024,
    )
    job = PasteJob("player", "new-world", plan, "Overworld", anchor, 90, capture_history=True)
    plugin = _plugin("skip")
    plugin._paste_budget = 1200
    plugin._paste_change_budget = 256
    plugin._chunk_requests_per_tick = 1
    plugin._auto_load_chunks = True
    plugin._chunk_load_timeout = 40
    plugin._chunk_stabilize_ticks = 2
    plugin._legacy_ticket_slots = {}
    plugin._tick_counter = 0
    plugin._progress_interval = 40
    plugin._history_max_blocks_per_operation = 10_000
    plugin.server.get_player = lambda _uuid: None
    plugin.paste_jobs = {job.player_uuid: job}
    monkeypatch.setitem(plugin._process_paste_jobs.__func__.__globals__, "monotonic", lambda: 0)
    loads = []
    releases = []
    ready = {}

    class NewWorld(Dimension):
        def load_chunk(self, x, z):
            loads.append((x, z))
            ready[(x, z)] = plugin._tick_counter + 3
            return True

        def is_chunk_loaded(self, x, z):
            return (x, z) in ready and plugin._tick_counter >= ready[(x, z)]

        def get_block_at(self, x, y, z):
            assert self.is_chunk_loaded(x // 16, z // 16), "world access before chunk generation"
            return super().get_block_at(x, y, z)

        def unload_chunk_request(self, x, z):
            releases.append((x, z))
            ready.pop((x, z))
            return True

    dimension = NewWorld()
    plugin._get_dimension = lambda _identifier: dimension
    plugin._complete_paste_job = lambda completed: plugin.paste_jobs.pop(completed.player_uuid)

    def fail(_job, reason):
        raise AssertionError(reason)

    plugin._fail_paste_job = fail
    for tick in range(200):
        plugin._tick_counter = tick
        previous = job.write_attempts
        plugin._process_paste_jobs()
        assert job.write_attempts - previous <= 256
        if not plugin.paste_jobs:
            break
    assert not plugin.paste_jobs, "paste did not finish"
    assert job.cursor == job.placed == job.captured_blocks == schematic.block_count
    assert plugin._paste_integrity_error(job) is None
    assert len(loads) == len(set(loads)) == len(plan.chunks)
    assert releases == loads
    assert not ready
    assert set(dimension.blocks) == {
        (anchor.x + dx, anchor.y + dy, anchor.z + dz) for dx, dy, dz, _ in iter_records(plan.records)
    }
    assert all(block.data.type.id == "minecraft:stone" for block in dimension.blocks.values())
    history = plugin._history_entry_from_job(job)
    assert history.block_count == schematic.block_count
    assert history.before_plan.palette == [{"type": "minecraft:air", "states": {}}]
    assert history.after_plan.palette == schematic.palette
    history.before_plan.records.close()
    history.after_plan.records.close()
    plan.records.close()
