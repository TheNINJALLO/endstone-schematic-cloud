from endstone_ninjos_schematics.codec import append_record, iter_records
from endstone_ninjos_schematics.models import (
    BlockPos, HistoryEntry, PasteChunkRange, PastePlan
)


def _plan(block_type: str) -> PastePlan:
    records = bytearray()
    append_record(records, 0, 0, 0, 0)
    append_record(records, 1, 0, 0, 0)
    return PastePlan(
        size=(2, 1, 1),
        palette=[{"type": block_type, "states": {}}],
        records=bytes(records),
        chunks=(PasteChunkRange(0, 0, 0, 2),),
    )


def test_history_entry_keeps_matching_before_after_coordinates():
    before = _plan("minecraft:dirt")
    after = _plan("minecraft:stone")
    entry = HistoryEntry(
        name="test",
        dimension_id="Overworld",
        anchor=BlockPos(10, 64, 10),
        before_plan=before,
        after_plan=after,
        block_count=2,
    )
    assert entry.block_count == before.block_count == after.block_count == 2
    assert [(x, y, z) for x, y, z, _ in iter_records(before.records)] == [
        (x, y, z) for x, y, z, _ in iter_records(after.records)
    ]


def test_history_chunk_range_is_bounded_to_record_count():
    plan = _plan("minecraft:stone")
    chunk = plan.chunks[0]
    assert chunk.start == 0
    assert chunk.end == plan.block_count
    assert chunk.block_count == 2
