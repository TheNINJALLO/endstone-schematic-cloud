"""Ninj-OS schematic binary codec.

The database payload is a zlib-compressed stream containing a JSON header,
a palette, and fixed-width relative block records. Both in-memory and streaming
paths use the same version-2 wire format. Version 1 payloads remain readable.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import DecodedSchematic
from .record_store import SpillRecordBuffer

MAGIC = b"NSCM"
FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1
PREFIX = struct.Struct("<4sBII")
RECORD = struct.Struct("<IIII")
MAX_HEADER_BYTES = 32 * 1024 * 1024
MAX_BLOCK_ENTITY_BYTES = 512 * 1024 * 1024


class SchematicCodecError(ValueError):
    """Raised when a schematic payload is invalid or unsupported."""


@dataclass(frozen=True, slots=True)
class EncodedSchematic:
    payload: bytes
    sha256_hex: str
    compressed_bytes: int
    uncompressed_bytes: int


@dataclass(frozen=True, slots=True)
class EncodedSchematicFile:
    path: Path
    sha256_hex: str
    compressed_bytes: int
    uncompressed_bytes: int


def palette_key(block_type: str, states: dict[str, bool | str | int]) -> str:
    return json.dumps(
        [block_type, sorted(states.items())],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def append_record(buffer: Any, dx: int, dy: int, dz: int, palette_index: int) -> None:
    for value in (dx, dy, dz, palette_index):
        if value < 0 or value > 0xFFFFFFFF:
            raise SchematicCodecError("record value is outside uint32 range")
    buffer.extend(RECORD.pack(dx, dy, dz, palette_index))


def _encode_block_entities(
    block_entities: dict[tuple[int, int, int], dict[str, Any]] | None,
) -> tuple[bytes, int]:
    entries: list[list[Any]] = []
    for position, payload in sorted((block_entities or {}).items()):
        if (
            not isinstance(position, tuple)
            or len(position) != 3
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in position)
        ):
            raise SchematicCodecError("block-entity position must contain three non-negative integers")
        if not isinstance(payload, dict):
            raise SchematicCodecError("block-entity payload must be a mapping")
        entries.append([position[0], position[1], position[2], payload])
    if not entries:
        return b"", 0
    try:
        encoded = json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchematicCodecError(f"block-entity data is not JSON-safe: {exc}") from exc
    if len(encoded) > MAX_BLOCK_ENTITY_BYTES:
        raise SchematicCodecError("block-entity data exceeds the safety limit")
    return encoded, len(entries)


def _decode_block_entities(
    encoded: bytes,
    expected_count: int,
) -> dict[tuple[int, int, int], dict[str, Any]]:
    if not encoded:
        if expected_count:
            raise SchematicCodecError("block-entity section is missing")
        return {}
    try:
        entries = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchematicCodecError(f"invalid block-entity section: {exc}") from exc
    if not isinstance(entries, list) or len(entries) != expected_count:
        raise SchematicCodecError("block-entity count does not match payload")
    result: dict[tuple[int, int, int], dict[str, Any]] = {}
    for entry in entries:
        if (
            not isinstance(entry, list)
            or len(entry) != 4
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in entry[:3])
            or not isinstance(entry[3], dict)
        ):
            raise SchematicCodecError("invalid block-entity entry")
        position = int(entry[0]), int(entry[1]), int(entry[2])
        if position in result:
            raise SchematicCodecError("duplicate block-entity position")
        result[position] = entry[3]
    return result


def _normalized_header(
    header: dict[str, Any],
    palette: list[dict[str, Any]],
    block_count: int,
    block_entity_bytes: int,
    block_entity_count: int,
) -> bytes:
    normalized = dict(header)
    normalized["format_version"] = FORMAT_VERSION
    normalized["block_count"] = int(block_count)
    normalized["palette_count"] = len(palette)
    normalized["palette"] = palette
    normalized["block_entity_encoding"] = "json-v1"
    normalized["block_entity_bytes"] = int(block_entity_bytes)
    normalized["block_entity_count"] = int(block_entity_count)
    header_bytes = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise SchematicCodecError("schematic header is too large")
    return header_bytes


def record_byte_length(records: Any) -> int:
    try:
        return len(records)
    except TypeError as exc:
        raise SchematicCodecError("record source does not expose a byte length") from exc


def iter_record_bytes(records: Any, chunk_bytes: int = 1024 * 1024) -> Iterator[bytes]:
    if hasattr(records, "iter_chunks"):
        yield from records.iter_chunks(chunk_bytes)
        return
    view = memoryview(records)
    for offset in range(0, len(view), max(RECORD.size, int(chunk_bytes))):
        yield bytes(view[offset : offset + chunk_bytes])


def iter_records(
    records: Any,
    start_index: int = 0,
    count: int | None = None,
    *,
    chunk_records: int = 65536,
) -> Iterator[tuple[int, int, int, int]]:
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if hasattr(records, "iter_records"):
        yield from records.iter_records(
            RECORD,
            start_index=start_index,
            count=count,
            chunk_records=chunk_records,
        )
        return
    offset = start_index * RECORD.size
    view = memoryview(records)
    if offset > len(view):
        return
    end = len(view) if count is None else min(len(view), offset + max(0, int(count)) * RECORD.size)
    yield from RECORD.iter_unpack(view[offset:end])


def record_at(records: Any, index: int) -> tuple[int, int, int, int]:
    if hasattr(records, "unpack_record"):
        return records.unpack_record(RECORD, index)
    return RECORD.unpack_from(records, int(index) * RECORD.size)


def encode_schematic(
    header: dict[str, Any],
    palette: list[dict[str, Any]],
    records: Any,
    compression_level: int = 6,
    *,
    block_entities: dict[tuple[int, int, int], dict[str, Any]] | None = None,
) -> EncodedSchematic:
    byte_length = record_byte_length(records)
    if byte_length % RECORD.size != 0:
        raise SchematicCodecError("record byte length is not aligned")
    block_count = byte_length // RECORD.size
    block_entity_data, block_entity_count = _encode_block_entities(block_entities)
    header_bytes = _normalized_header(
        header, palette, block_count, len(block_entity_data), block_entity_count
    )
    prefix = PREFIX.pack(MAGIC, FORMAT_VERSION, len(header_bytes), block_count)
    level = max(0, min(9, int(compression_level)))
    compressor = zlib.compressobj(level)
    output = bytearray()
    output.extend(compressor.compress(prefix))
    output.extend(compressor.compress(header_bytes))
    output.extend(compressor.compress(block_entity_data))
    for piece in iter_record_bytes(records):
        output.extend(compressor.compress(piece))
    output.extend(compressor.flush())
    payload = bytes(output)
    return EncodedSchematic(
        payload=payload,
        sha256_hex=hashlib.sha256(payload).hexdigest(),
        compressed_bytes=len(payload),
        uncompressed_bytes=len(prefix) + len(header_bytes) + len(block_entity_data) + byte_length,
    )


def encode_schematic_to_file(
    header: dict[str, Any],
    palette: list[dict[str, Any]],
    records: Any,
    destination: Path,
    compression_level: int = 6,
    *,
    block_entities: dict[tuple[int, int, int], dict[str, Any]] | None = None,
    chunk_bytes: int = 1024 * 1024,
) -> EncodedSchematicFile:
    """Stream one NSCM payload to disk without creating whole-payload copies."""

    byte_length = record_byte_length(records)
    if byte_length % RECORD.size != 0:
        raise SchematicCodecError("record byte length is not aligned")
    block_count = byte_length // RECORD.size
    block_entity_data, block_entity_count = _encode_block_entities(block_entities)
    header_bytes = _normalized_header(
        header, palette, block_count, len(block_entity_data), block_entity_count
    )
    prefix = PREFIX.pack(MAGIC, FORMAT_VERSION, len(header_bytes), block_count)
    level = max(0, min(9, int(compression_level)))
    compressor = zlib.compressobj(level)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0

    def emit(stream: Any, piece: bytes) -> None:
        nonlocal written
        if not piece:
            return
        stream.write(piece)
        digest.update(piece)
        written += len(piece)

    with destination.open("wb") as stream:
        emit(stream, compressor.compress(prefix))
        emit(stream, compressor.compress(header_bytes))
        emit(stream, compressor.compress(block_entity_data))
        for piece in iter_record_bytes(records, chunk_bytes):
            emit(stream, compressor.compress(piece))
        emit(stream, compressor.flush())
        stream.flush()
    return EncodedSchematicFile(
        path=destination,
        sha256_hex=digest.hexdigest(),
        compressed_bytes=written,
        uncompressed_bytes=len(prefix) + len(header_bytes) + len(block_entity_data) + byte_length,
    )


def _validate_decoded_header(
    header: dict[str, Any], palette: Any, block_count: int
) -> list[dict[str, Any]]:
    if not isinstance(palette, list):
        raise SchematicCodecError("palette is missing")
    if int(header.get("block_count", -1)) != block_count:
        raise SchematicCodecError("header block count does not match payload")
    for entry in palette:
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
            raise SchematicCodecError("invalid palette entry")
        if not isinstance(entry.get("states", {}), dict):
            raise SchematicCodecError("invalid palette block states")
    return palette


def _block_entity_section_fields(
    header: dict[str, Any], wire_version: int, block_count: int
) -> tuple[int, int]:
    declared_version = header.get("format_version")
    if (
        not isinstance(declared_version, int)
        or isinstance(declared_version, bool)
        or declared_version != wire_version
    ):
        raise SchematicCodecError("header format version does not match payload prefix")
    if wire_version == LEGACY_FORMAT_VERSION:
        return 0, 0
    if header.get("block_entity_encoding") != "json-v1":
        raise SchematicCodecError("unsupported block-entity encoding")
    byte_length = header.get("block_entity_bytes")
    entity_count = header.get("block_entity_count")
    if (
        not isinstance(byte_length, int)
        or isinstance(byte_length, bool)
        or byte_length < 0
        or byte_length > MAX_BLOCK_ENTITY_BYTES
    ):
        raise SchematicCodecError("block-entity section length exceeds safety limit")
    if (
        not isinstance(entity_count, int)
        or isinstance(entity_count, bool)
        or entity_count < 0
        or entity_count > block_count
    ):
        raise SchematicCodecError("block-entity count exceeds block record count")
    return byte_length, entity_count


def decode_schematic(payload: bytes, expected_sha256: str | None = None) -> DecodedSchematic:
    if expected_sha256:
        actual = hashlib.sha256(payload).hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise SchematicCodecError("schematic checksum mismatch")
    try:
        raw = zlib.decompress(payload)
    except zlib.error as exc:
        raise SchematicCodecError(f"invalid compressed payload: {exc}") from exc
    if len(raw) < PREFIX.size:
        raise SchematicCodecError("payload is truncated")
    magic, version, header_len, block_count = PREFIX.unpack_from(raw, 0)
    if magic != MAGIC:
        raise SchematicCodecError("invalid schematic magic")
    if version not in {LEGACY_FORMAT_VERSION, FORMAT_VERSION}:
        raise SchematicCodecError(f"unsupported schematic version: {version}")
    if header_len > MAX_HEADER_BYTES:
        raise SchematicCodecError("header length exceeds safety limit")
    header_start = PREFIX.size
    header_end = header_start + header_len
    if header_end > len(raw):
        raise SchematicCodecError("payload header is truncated")
    try:
        header = json.loads(raw[header_start:header_end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchematicCodecError(f"invalid schematic header: {exc}") from exc
    block_entity_len, block_entity_count = _block_entity_section_fields(
        header, version, block_count
    )
    block_entity_end = header_end + block_entity_len
    if block_entity_end > len(raw):
        raise SchematicCodecError("block-entity section is truncated")
    block_entities = _decode_block_entities(raw[header_end:block_entity_end], block_entity_count)
    records = raw[block_entity_end:]
    expected_record_bytes = block_count * RECORD.size
    if len(records) != expected_record_bytes:
        raise SchematicCodecError(
            f"record byte count mismatch: expected {expected_record_bytes}, got {len(records)}"
        )
    palette = _validate_decoded_header(header, header.pop("palette", None), block_count)
    return DecodedSchematic(
        header=header,
        palette=palette,
        records=records,
        block_entities=block_entities,
    )


def decode_schematic_file(
    payload_path: Path,
    expected_sha256: str | None,
    record_buffer_factory: Callable[[str], SpillRecordBuffer],
    *,
    compressed_chunk_bytes: int = 1024 * 1024,
) -> DecodedSchematic:
    """Stream-decode one payload file into a bounded-memory record source."""

    payload_path = Path(payload_path)
    digest = hashlib.sha256()
    decompressor = zlib.decompressobj()
    prefix_buffer = bytearray()
    header_len: int | None = None
    block_count: int | None = None
    wire_version: int | None = None
    header: dict[str, Any] | None = None
    palette: list[dict[str, Any]] | None = None
    block_entities: dict[tuple[int, int, int], dict[str, Any]] | None = None
    records = record_buffer_factory("decoded-")

    def consume(data: bytes) -> None:
        nonlocal header_len, block_count, wire_version, header, palette, block_entities
        if not data:
            return
        if header is not None:
            records.extend(data)
            return
        prefix_buffer.extend(data)
        if header_len is None and len(prefix_buffer) >= PREFIX.size:
            magic, version, parsed_header_len, parsed_block_count = PREFIX.unpack_from(prefix_buffer, 0)
            if magic != MAGIC:
                raise SchematicCodecError("invalid schematic magic")
            if version not in {LEGACY_FORMAT_VERSION, FORMAT_VERSION}:
                raise SchematicCodecError(f"unsupported schematic version: {version}")
            if parsed_header_len > MAX_HEADER_BYTES:
                raise SchematicCodecError("header length exceeds safety limit")
            header_len = int(parsed_header_len)
            block_count = int(parsed_block_count)
            wire_version = int(version)
        if header_len is None or len(prefix_buffer) < PREFIX.size + header_len:
            return
        header_start = PREFIX.size
        header_end = header_start + header_len
        try:
            parsed_header = json.loads(prefix_buffer[header_start:header_end].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchematicCodecError(f"invalid schematic header: {exc}") from exc
        parsed_palette = _validate_decoded_header(
            parsed_header, parsed_header.pop("palette", None), int(block_count)
        )
        assert wire_version is not None
        block_entity_len, block_entity_count = _block_entity_section_fields(
            parsed_header, wire_version, int(block_count)
        )
        block_entity_end = header_end + block_entity_len
        if len(prefix_buffer) < block_entity_end:
            return
        parsed_block_entities = _decode_block_entities(
            bytes(prefix_buffer[header_end:block_entity_end]), block_entity_count
        )
        header = parsed_header
        palette = parsed_palette
        block_entities = parsed_block_entities
        trailing = bytes(prefix_buffer[block_entity_end:])
        prefix_buffer.clear()
        if trailing:
            records.extend(trailing)

    try:
        with payload_path.open("rb") as stream:
            while True:
                piece = stream.read(max(4096, int(compressed_chunk_bytes)))
                if not piece:
                    break
                digest.update(piece)
                pending = piece
                while pending:
                    consume(decompressor.decompress(pending, 1024 * 1024))
                    pending = decompressor.unconsumed_tail
                # Drain any output buffered after the input was consumed without
                # allowing one highly-compressible chunk to inflate into a giant bytes object.
                while True:
                    drained = decompressor.decompress(b"", 1024 * 1024)
                    if not drained:
                        break
                    consume(drained)
            consume(decompressor.flush(1024 * 1024))
        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
            raise SchematicCodecError("schematic checksum mismatch")
        if not decompressor.eof:
            raise SchematicCodecError("compressed payload ended before the zlib stream completed")
        if header is None or palette is None or block_count is None or block_entities is None:
            raise SchematicCodecError("payload header is truncated")
        expected_record_bytes = block_count * RECORD.size
        if len(records) != expected_record_bytes:
            raise SchematicCodecError(
                f"record byte count mismatch: expected {expected_record_bytes}, got {len(records)}"
            )
        source = records.freeze()
        return DecodedSchematic(
            header=header,
            palette=palette,
            records=source,
            block_entities=block_entities,
        )
    except Exception:
        records.close()
        raise
