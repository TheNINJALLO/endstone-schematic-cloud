"""Bounded-memory storage for fixed-width schematic records.

Small buffers remain in memory. Once a configurable threshold is crossed the
buffer spills to a temporary file and subsequent reads are streamed from disk.
This prevents multi-million-block schematics from multiplying into several large
Python byte objects during save, decode, paste planning, and undo capture.
"""

from __future__ import annotations

import os
import struct
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator

RECORD_SIZE = 16


class RecordStoreError(RuntimeError):
    """Record-store operation failed."""


class RecordSource:
    """Immutable record bytes backed by either memory or one temporary file."""

    __slots__ = (
        "_data", "_path", "_length", "_delete_on_close", "_closed",
        "_reader", "_cache_start", "_cache"
    )

    def __init__(
        self,
        *,
        data: bytes | None = None,
        path: Path | None = None,
        length: int | None = None,
        delete_on_close: bool = True,
    ) -> None:
        if (data is None) == (path is None):
            raise ValueError("RecordSource requires exactly one of data or path")
        if data is not None:
            self._data = bytes(data)
            self._path = None
            self._length = len(self._data)
        else:
            resolved = Path(path)  # type: ignore[arg-type]
            self._data = None
            self._path = resolved
            self._length = int(resolved.stat().st_size if length is None else length)
        if self._length % RECORD_SIZE:
            raise RecordStoreError("record source byte length is not aligned")
        self._delete_on_close = bool(delete_on_close)
        self._closed = False
        self._reader: BinaryIO | None = None
        self._cache_start = -1
        self._cache = b""

    def __len__(self) -> int:
        return self._length

    @property
    def record_count(self) -> int:
        return self._length // RECORD_SIZE

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def is_file_backed(self) -> bool:
        return self._path is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def iter_chunks(
        self,
        chunk_bytes: int = 1024 * 1024,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> Iterator[bytes]:
        if self._closed:
            raise RecordStoreError("record source is closed")
        chunk_bytes = max(RECORD_SIZE, int(chunk_bytes))
        offset = max(0, int(offset))
        if offset > self._length:
            return
        remaining = self._length - offset if length is None else min(max(0, int(length)), self._length - offset)
        if self._data is not None:
            end = offset + remaining
            for position in range(offset, end, chunk_bytes):
                yield self._data[position : min(end, position + chunk_bytes)]
            return
        assert self._path is not None
        with self._path.open("rb") as stream:
            stream.seek(offset)
            while remaining > 0:
                piece = stream.read(min(chunk_bytes, remaining))
                if not piece:
                    raise RecordStoreError("record source ended before its declared length")
                remaining -= len(piece)
                yield piece

    def iter_records(
        self,
        record_struct: struct.Struct,
        *,
        start_index: int = 0,
        count: int | None = None,
        chunk_records: int = 65536,
    ) -> Iterator[tuple[int, ...]]:
        if record_struct.size != RECORD_SIZE:
            raise ValueError("record struct size mismatch")
        start_index = max(0, int(start_index))
        available = max(0, self.record_count - start_index)
        wanted = available if count is None else min(available, max(0, int(count)))
        if wanted <= 0:
            return
        carry = b""
        byte_count = wanted * RECORD_SIZE
        for piece in self.iter_chunks(
            max(RECORD_SIZE, int(chunk_records) * RECORD_SIZE),
            offset=start_index * RECORD_SIZE,
            length=byte_count,
        ):
            data = carry + piece
            aligned = len(data) - (len(data) % RECORD_SIZE)
            for record in record_struct.iter_unpack(data[:aligned]):
                yield record
            carry = data[aligned:]
        if carry:
            raise RecordStoreError("record source ended on a partial record")

    def unpack_record(self, record_struct: struct.Struct, index: int) -> tuple[int, ...]:
        if self._closed:
            raise RecordStoreError("record source is closed")
        index = int(index)
        if index < 0 or index >= self.record_count:
            raise IndexError(index)
        offset = index * RECORD_SIZE
        if self._data is not None:
            return record_struct.unpack_from(self._data, offset)
        assert self._path is not None
        cache_bytes = 1024 * 1024
        if not (self._cache_start <= offset and offset + RECORD_SIZE <= self._cache_start + len(self._cache)):
            if self._reader is None:
                self._reader = self._path.open("rb", buffering=1024 * 1024)
            cache_start = (offset // cache_bytes) * cache_bytes
            self._reader.seek(cache_start)
            self._cache = self._reader.read(cache_bytes)
            self._cache_start = cache_start
        local = offset - self._cache_start
        data = self._cache[local : local + RECORD_SIZE]
        if len(data) != RECORD_SIZE:
            raise RecordStoreError("record source is truncated")
        return record_struct.unpack(data)

    def to_bytes(self, *, maximum_bytes: int | None = None) -> bytes:
        if maximum_bytes is not None and self._length > int(maximum_bytes):
            raise RecordStoreError(
                f"record source is {self._length:,} bytes, above the allowed in-memory copy limit"
            )
        if self._data is not None:
            return self._data
        return b"".join(self.iter_chunks())

    def close(self, *, delete: bool | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
            self._reader = None
        self._cache = b""
        self._cache_start = -1
        should_delete = self._delete_on_close if delete is None else bool(delete)
        if should_delete and self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass

    def __del__(self) -> None:  # pragma: no cover - best-effort crash cleanup
        try:
            self.close()
        except Exception:
            pass


class SpillRecordBuffer:
    """Mutable record byte buffer that spills to disk after a threshold."""

    __slots__ = (
        "_memory",
        "_stream",
        "_path",
        "_length",
        "_threshold",
        "_directory",
        "_prefix",
        "_closed",
    )

    def __init__(
        self,
        directory: Path,
        *,
        threshold_bytes: int = 8 * 1024 * 1024,
        prefix: str = "records-",
    ) -> None:
        self._memory = bytearray()
        self._stream: BinaryIO | None = None
        self._path: Path | None = None
        self._length = 0
        self._threshold = max(RECORD_SIZE, int(threshold_bytes))
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._prefix = prefix
        self._closed = False

    def __len__(self) -> int:
        return self._length

    @property
    def record_count(self) -> int:
        return self._length // RECORD_SIZE

    @property
    def is_file_backed(self) -> bool:
        return self._stream is not None or self._path is not None

    @property
    def path(self) -> Path | None:
        return self._path

    def _ensure_open(self) -> None:
        if self._closed:
            raise RecordStoreError("record buffer is closed")

    def _spill(self) -> None:
        if self._stream is not None:
            return
        handle = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=self._prefix,
            suffix=".bin",
            dir=self._directory,
            delete=False,
        )
        self._stream = handle
        self._path = Path(handle.name)
        if self._memory:
            handle.write(self._memory)
            self._memory.clear()

    def extend(self, data: bytes | bytearray | memoryview) -> None:
        self._ensure_open()
        piece = bytes(data)
        if not piece:
            return
        if self._stream is None and self._length + len(piece) > self._threshold:
            self._spill()
        if self._stream is not None:
            self._stream.write(piece)
        else:
            self._memory.extend(piece)
        self._length += len(piece)

    def truncate(self, byte_length: int) -> None:
        self._ensure_open()
        byte_length = max(0, int(byte_length))
        if byte_length > self._length:
            raise ValueError("cannot grow a record buffer with truncate")
        if byte_length % RECORD_SIZE:
            raise RecordStoreError("record buffer truncation is not record-aligned")
        if self._stream is not None:
            self._stream.flush()
            self._stream.truncate(byte_length)
            self._stream.seek(byte_length)
        else:
            del self._memory[byte_length:]
        self._length = byte_length

    def clear(self) -> None:
        self.truncate(0)

    def freeze(self) -> RecordSource:
        self._ensure_open()
        if self._length % RECORD_SIZE:
            raise RecordStoreError("record buffer byte length is not aligned")
        if self._stream is None:
            source = RecordSource(data=bytes(self._memory))
        else:
            self._stream.flush()
            try:
                os.fsync(self._stream.fileno())
            except OSError:
                pass
            self._stream.close()
            self._stream = None
            assert self._path is not None
            source = RecordSource(path=self._path, length=self._length, delete_on_close=True)
            self._path = None
        self._memory.clear()
        self._length = 0
        self._closed = True
        return source

    def close(self, *, delete: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        if delete and self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
        self._path = None
        self._memory.clear()
        self._length = 0

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


def cleanup_orphan_record_files(
    directory: Path,
    prefixes: tuple[str, ...] = ("records-", "decoded-", "plan-", "payload-"),
) -> int:
    """Delete temporary files from a previous unclean shutdown."""

    directory = Path(directory)
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.iterdir():
        if not path.is_file() or not any(path.name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
