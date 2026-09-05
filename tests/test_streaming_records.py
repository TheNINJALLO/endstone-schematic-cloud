from pathlib import Path

from endstone_ninjos_schematics.codec import (
    RECORD,
    append_record,
    decode_schematic_file,
    encode_schematic_to_file,
    iter_records,
)
from endstone_ninjos_schematics.models import BlockPos, DecodedSchematic
from endstone_ninjos_schematics.planner import prepare_paste_plan, prepare_streaming_paste_plan
from endstone_ninjos_schematics.record_store import SpillRecordBuffer


def factory(tmp_path: Path, threshold: int = 64):
    return lambda prefix: SpillRecordBuffer(tmp_path, threshold_bytes=threshold, prefix=prefix)


def test_spill_buffer_truncate_freeze_and_cached_random_reads(tmp_path):
    buffer = SpillRecordBuffer(tmp_path, threshold_bytes=32, prefix="records-test-")
    for index in range(20):
        append_record(buffer, index, index + 1, index + 2, index % 3)
    assert buffer.is_file_backed
    assert len(buffer) == 20 * RECORD.size
    buffer.truncate(12 * RECORD.size)
    source = buffer.freeze()
    assert source.is_file_backed
    assert source.record_count == 12
    assert source.unpack_record(RECORD, 0) == (0, 1, 2, 0)
    assert source.unpack_record(RECORD, 11) == (11, 12, 13, 2)
    assert list(source.iter_records(RECORD))[5] == (5, 6, 7, 2)
    path = source.path
    source.close()
    assert path is not None and not path.exists()


def test_streaming_codec_round_trip_uses_file_backed_records(tmp_path):
    records = SpillRecordBuffer(tmp_path, threshold_bytes=128, prefix="records-source-")
    count = 50_000
    for index in range(count):
        append_record(records, index % 100, (index // 100) % 50, index % 80, index % 2)
    source = records.freeze()
    payload_path = tmp_path / "large.nscm"
    encoded = encode_schematic_to_file(
        {
            "name": "large",
            "size": [100, 50, 80],
            "selection_volume": 400_000,
            "includes_air": False,
        },
        [
            {"type": "minecraft:stone", "states": {}},
            {"type": "minecraft:dirt", "states": {}},
        ],
        source,
        payload_path,
        6,
        block_entities={
            (99, 49, 79): {
                "schema": 1,
                "actor_type": "minecraft:chest",
                "canonical_nbt": True,
                "is_container": True,
                "container_size": 27,
                "nbt": {},
                "inventory": [],
            }
        },
    )
    decoded = decode_schematic_file(
        payload_path,
        encoded.sha256_hex,
        factory(tmp_path, threshold=128),
        compressed_chunk_bytes=257,
    )
    assert decoded.block_count == count
    assert decoded.records.is_file_backed
    assert (99, 49, 79) in decoded.block_entities
    assert list(iter_records(decoded.records, 49_999, 1)) == [(99, 49, 79, 1)]
    source.close()
    decoded.records.close()


def test_streaming_planner_matches_in_memory_record_set(tmp_path):
    raw = bytearray()
    for y in range(8):
        for z in range(24):
            for x in range(24):
                append_record(raw, x, y, z, (x + z) % 2)
    schematic_memory = DecodedSchematic(
        header={
            "size": [24, 8, 24],
            "block_count": len(raw) // RECORD.size,
            "selection_volume": 24 * 8 * 24,
            "includes_air": True,
        },
        palette=[
            {"type": "minecraft:stone", "states": {}},
            {"type": "minecraft:dirt", "states": {}},
        ],
        records=bytes(raw),
    )
    source_buffer = SpillRecordBuffer(tmp_path, threshold_bytes=64, prefix="records-plan-source-")
    source_buffer.extend(raw)
    schematic_stream = DecodedSchematic(
        header=dict(schematic_memory.header),
        palette=list(schematic_memory.palette),
        records=source_buffer.freeze(),
    )
    anchor = BlockPos(7, 20, -11)
    expected = prepare_paste_plan(schematic_memory, anchor, 90)
    actual = prepare_streaming_paste_plan(
        schematic_stream,
        anchor,
        90,
        factory(tmp_path, threshold=64),
        batch_records=1024,
    )
    assert actual.block_count == expected.block_count
    assert actual.size == expected.size
    assert sorted(iter_records(actual.records)) == sorted(iter_records(expected.records))
    assert sum(chunk.block_count for chunk in actual.chunks) == actual.block_count
    schematic_stream.records.close()
    actual.records.close()
