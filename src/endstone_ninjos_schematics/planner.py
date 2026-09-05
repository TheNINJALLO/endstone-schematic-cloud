"""Pure helpers for chunk-aware schematic scanning and bounded-memory paste planning."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from .codec import RECORD, append_record, iter_records, record_byte_length
from .models import BlockPos, ChunkRegion, DecodedSchematic, PasteChunkRange, PastePlan
from .record_store import SpillRecordBuffer
from .rotation import normalize_rotation, rotate_coord, rotate_states, rotated_size


def build_chunk_regions(low: BlockPos, size: tuple[int, int, int]) -> tuple[ChunkRegion, ...]:
    """Split a cuboid into deterministic chunk-contained regions."""
    sx, sy, sz = size
    high_x = low.x + sx - 1
    high_y = low.y + sy - 1
    high_z = low.z + sz - 1
    regions: list[ChunkRegion] = []
    for chunk_z in range(low.z // 16, high_z // 16 + 1):
        min_z = max(low.z, chunk_z * 16)
        max_z = min(high_z, chunk_z * 16 + 15)
        for chunk_x in range(low.x // 16, high_x // 16 + 1):
            min_x = max(low.x, chunk_x * 16)
            max_x = min(high_x, chunk_x * 16 + 15)
            regions.append(
                ChunkRegion(
                    chunk_x=chunk_x,
                    chunk_z=chunk_z,
                    min_x=min_x,
                    max_x=max_x,
                    min_z=min_z,
                    max_z=max_z,
                    min_y=low.y,
                    max_y=high_y,
                )
            )
    return tuple(regions)


def validate_schematic_integrity(schematic: DecodedSchematic) -> dict[str, int | bool]:
    """Validate dimensions, record coverage, coordinates, and palette references."""

    sx, sy, sz = schematic.size
    if sx <= 0 or sy <= 0 or sz <= 0:
        raise ValueError(f"invalid schematic size: {sx}x{sy}x{sz}")
    volume = sx * sy * sz
    count = schematic.block_count
    byte_count = record_byte_length(schematic.records)
    if count != byte_count // RECORD.size:
        raise ValueError("schematic header block count does not match record buffer")
    if byte_count % RECORD.size:
        raise ValueError("schematic record buffer is not aligned")
    selection_volume = int(schematic.header.get("selection_volume", volume))
    if selection_volume != volume:
        raise ValueError(
            f"schematic selection volume mismatch: header={selection_volume}, dimensions={volume}"
        )
    includes_air = bool(schematic.header.get("includes_air", False))
    if includes_air and count != volume:
        raise ValueError(
            f"full-volume schematic is incomplete: expected {volume:,} records, got {count:,}"
        )
    if count > volume:
        raise ValueError(f"schematic has {count:,} records for a {volume:,}-block volume")

    unmatched_entities = set(schematic.block_entities)
    for index, (dx, dy, dz, palette_index) in enumerate(iter_records(schematic.records)):
        unmatched_entities.discard((dx, dy, dz))
        if dx >= sx or dy >= sy or dz >= sz:
            raise ValueError(
                f"record {index:,} is outside schematic bounds: ({dx}, {dy}, {dz}) "
                f"not within {sx}x{sy}x{sz}"
            )
        if palette_index >= len(schematic.palette):
            raise ValueError(
                f"record {index:,} references missing palette index {palette_index}"
            )
    for dx, dy, dz in schematic.block_entities:
        if dx >= sx or dy >= sy or dz >= sz:
            raise ValueError(
                f"block entity is outside schematic bounds: ({dx}, {dy}, {dz}) "
                f"not within {sx}x{sy}x{sz}"
            )
    if unmatched_entities:
        dx, dy, dz = min(unmatched_entities)
        raise ValueError(
            f"block entity at ({dx}, {dy}, {dz}) has no matching block record"
        )
    return {
        "volume": volume,
        "block_count": count,
        "palette_count": len(schematic.palette),
        "includes_air": includes_air,
    }


def _append_chunk_range(
    ranges: list[PasteChunkRange], chunk_x: int, chunk_z: int, start: int, end: int
) -> None:
    if start == end:
        return
    if ranges and ranges[-1].chunk_x == chunk_x and ranges[-1].chunk_z == chunk_z and ranges[-1].end == start:
        previous = ranges[-1]
        ranges[-1] = PasteChunkRange(chunk_x, chunk_z, previous.start, end)
    else:
        ranges.append(PasteChunkRange(chunk_x, chunk_z, start, end))


def prepare_paste_plan(
    schematic: DecodedSchematic,
    anchor: BlockPos,
    rotation: int,
) -> PastePlan:
    """In-memory planner retained for small schematics and compatibility tests."""
    validate_schematic_integrity(schematic)
    rotation = normalize_rotation(rotation)
    rotated_palette = [
        {"type": str(entry["type"]), "states": rotate_states(dict(entry.get("states", {})), rotation)}
        for entry in schematic.palette
    ]
    rotated_block_entities = {
        rotate_coord(dx, dy, dz, schematic.size, rotation): payload
        for (dx, dy, dz), payload in schematic.block_entities.items()
    }
    buckets: dict[tuple[int, int], bytearray] = defaultdict(bytearray)
    for dx, dy, dz, palette_index in iter_records(schematic.records):
        rx, ry, rz = rotate_coord(dx, dy, dz, schematic.size, rotation)
        chunk = ((anchor.x + rx) // 16, (anchor.z + rz) // 16)
        append_record(buckets[chunk], rx, ry, rz, palette_index)

    anchor_chunk = (anchor.x // 16, anchor.z // 16)
    ordered_chunks = sorted(
        buckets,
        key=lambda value: (
            (value[0] - anchor_chunk[0]) ** 2 + (value[1] - anchor_chunk[1]) ** 2,
            value[1],
            value[0],
        ),
    )
    records = bytearray()
    ranges: list[PasteChunkRange] = []
    cursor = 0
    for chunk_x, chunk_z in ordered_chunks:
        bucket = buckets[(chunk_x, chunk_z)]
        count = len(bucket) // RECORD.size
        records.extend(bucket)
        ranges.append(PasteChunkRange(chunk_x, chunk_z, cursor, cursor + count))
        cursor += count
    return PastePlan(
        size=rotated_size(schematic.size, rotation),
        palette=rotated_palette,
        records=bytes(records),
        chunks=tuple(ranges),
        block_entities=rotated_block_entities,
    )


def prepare_streaming_paste_plan(
    schematic: DecodedSchematic,
    anchor: BlockPos,
    rotation: int,
    record_buffer_factory: Callable[[str], SpillRecordBuffer],
    *,
    batch_records: int = 32768,
) -> PastePlan:
    """Build a disk-spill paste plan using bounded record batches.

    Each batch is grouped by destination chunk, appended to one spillable output
    stream, and then released. Chunks may appear in more than one range, but the
    operation never holds the full source and destination plans in RAM together.
    """

    validate_schematic_integrity(schematic)
    rotation = normalize_rotation(rotation)
    rotated_palette = [
        {"type": str(entry["type"]), "states": rotate_states(dict(entry.get("states", {})), rotation)}
        for entry in schematic.palette
    ]
    rotated_block_entities = {
        rotate_coord(dx, dy, dz, schematic.size, rotation): payload
        for (dx, dy, dz), payload in schematic.block_entities.items()
    }
    output = record_buffer_factory("plan-")
    ranges: list[PasteChunkRange] = []
    cursor = 0
    batch_records = max(1024, int(batch_records))
    anchor_chunk = (anchor.x // 16, anchor.z // 16)

    try:
        for batch_start in range(0, schematic.block_count, batch_records):
            count = min(batch_records, schematic.block_count - batch_start)
            buckets: dict[tuple[int, int], bytearray] = defaultdict(bytearray)
            for dx, dy, dz, palette_index in iter_records(
                schematic.records, batch_start, count, chunk_records=batch_records
            ):
                rx, ry, rz = rotate_coord(dx, dy, dz, schematic.size, rotation)
                chunk = ((anchor.x + rx) // 16, (anchor.z + rz) // 16)
                append_record(buckets[chunk], rx, ry, rz, palette_index)
            ordered = sorted(
                buckets,
                key=lambda value: (
                    (value[0] - anchor_chunk[0]) ** 2 + (value[1] - anchor_chunk[1]) ** 2,
                    value[1],
                    value[0],
                ),
            )
            for chunk_x, chunk_z in ordered:
                bucket = buckets[(chunk_x, chunk_z)]
                block_count = len(bucket) // RECORD.size
                output.extend(bucket)
                _append_chunk_range(ranges, chunk_x, chunk_z, cursor, cursor + block_count)
                cursor += block_count
        if cursor != schematic.block_count:
            raise ValueError(
                f"streaming paste planner produced {cursor:,} records from {schematic.block_count:,} inputs"
            )
        return PastePlan(
            size=rotated_size(schematic.size, rotation),
            palette=rotated_palette,
            records=output.freeze(),
            chunks=tuple(ranges),
            block_entities=rotated_block_entities,
        )
    except Exception:
        output.close()
        raise
