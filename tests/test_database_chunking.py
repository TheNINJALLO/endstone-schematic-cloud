import hashlib

import pytest

from endstone_ninjos_schematics.database import (
    DatabaseError,
    assemble_payload_chunks,
    iter_payload_chunks,
)


def _row(index, piece):
    return {
        "chunk_index": index,
        "chunk_bytes": len(piece),
        "chunk_sha256": hashlib.sha256(piece).hexdigest(),
        "payload": piece,
    }


def test_payload_is_split_below_packet_size_and_reassembled_exactly():
    payload = bytes(range(256)) * 20000
    pieces = list(iter_payload_chunks(payload, 1024 * 1024))
    assert len(pieces) == 5
    assert all(0 < len(piece) <= 1024 * 1024 for piece in pieces)
    rebuilt, count = assemble_payload_chunks(
        [_row(index, piece) for index, piece in enumerate(pieces)],
        expected_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert rebuilt == payload
    assert count == len(pieces)


def test_payload_chunk_gap_is_rejected():
    payload = b"abc" * 100
    with pytest.raises(DatabaseError, match="expected index 1"):
        assemble_payload_chunks(
            [_row(0, payload[:100]), _row(2, payload[100:])],
            expected_bytes=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_payload_chunk_corruption_is_rejected():
    piece = b"payload"
    row = _row(0, piece)
    row["chunk_sha256"] = "0" * 64
    with pytest.raises(DatabaseError, match="checksum mismatch"):
        assemble_payload_chunks(
            [row],
            expected_bytes=len(piece),
            expected_sha256=hashlib.sha256(piece).hexdigest(),
        )


def test_store_save_uses_small_chunk_queries_instead_of_one_large_blob():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from endstone_ninjos_schematics.database import MySQLSchematicStore

    class Cursor:
        def __init__(self):
            self.lastrowid = 7
            self.main_payload = None
            self.chunk_payloads = []
            self._verification = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            if sql.startswith("INSERT INTO `ninjos_schematics`"):
                self.main_payload = params[-1]
            elif sql.startswith("INSERT INTO `ninjos_schematic_payload_chunks`"):
                self.chunk_payloads.append(bytes(params[-1]))
            elif sql.startswith("SELECT COUNT(*)"):
                self._verification = {
                    "count": len(self.chunk_payloads),
                    "bytes": sum(map(len, self.chunk_payloads)),
                }
            return 1

        def fetchone(self):
            return self._verification

    class Connection:
        def __init__(self, cursor):
            self._cursor = cursor
            self.committed = False

        def cursor(self):
            return self._cursor

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("save should not roll back")

    cursor = Cursor()
    connection_obj = Connection(cursor)
    store = object.__new__(MySQLSchematicStore)
    store.table = "ninjos_schematics"
    store.chunk_table = "ninjos_schematic_payload_chunks"
    store.settings = SimpleNamespace(
        inline_payload_max_bytes=4,
        payload_chunk_bytes=4,
        retry_attempts=1,
        retry_backoff_seconds=0.1,
    )

    @contextmanager
    def connection(*, autocommit=True):
        assert autocommit is False
        yield connection_obj

    store._connection = connection
    payload = b"abcdefghij"
    row = {
        "namespace": "global",
        "name": "large",
        "display_name": "Large",
        "description": "",
        "author_uuid": "u",
        "author_xuid": "x",
        "author_name": "a",
        "source_server": "s",
        "source_dimension": "Overworld",
        "minecraft_version": "1",
        "plugin_version": "1",
        "format_version": 1,
        "size_x": 1,
        "size_y": 1,
        "size_z": 1,
        "block_count": 1,
        "non_air_count": 1,
        "palette_count": 1,
        "includes_air": 1,
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "compressed_bytes": len(payload),
        "uncompressed_bytes": 100,
        "payload": payload,
    }

    receipt = store.save(row, overwrite=False)
    assert receipt == {"storage": "chunked", "chunk_count": 3, "chunk_bytes": 4}
    assert cursor.main_payload == b""
    assert cursor.chunk_payloads == [b"abcd", b"efgh", b"ij"]
    assert connection_obj.committed is True


def test_store_fetch_reassembles_chunked_payload_and_checks_hash():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from endstone_ninjos_schematics.database import MySQLSchematicStore

    payload = b"abcdefghij"
    rows = [_row(index, piece) for index, piece in enumerate(iter_payload_chunks(payload, 4))]

    class Cursor:
        def __init__(self):
            self.mode = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.mode = "chunks" if "FROM `ninjos_schematic_payload_chunks`" in sql else "main"
            return 1

        def fetchone(self):
            return {
                "id": 9,
                "name": "large",
                "payload": b"",
                "compressed_bytes": len(payload),
                "content_sha256": hashlib.sha256(payload).hexdigest(),
            }

        def fetchall(self):
            assert self.mode == "chunks"
            return rows

    class Connection:
        def cursor(self):
            return Cursor()

    store = object.__new__(MySQLSchematicStore)
    store.table = "ninjos_schematics"
    store.chunk_table = "ninjos_schematic_payload_chunks"
    store.settings = SimpleNamespace(namespace="global")

    @contextmanager
    def connection(*, autocommit=True):
        yield Connection()

    store._connection = connection
    result = store.fetch("Large")
    assert result["payload"] == payload
    assert result["payload_storage"] == "chunked"
    assert result["payload_chunk_count"] == 3


def test_store_save_file_streams_chunked_payload_from_disk(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from endstone_ninjos_schematics.database import MySQLSchematicStore

    payload = b"abcdefghij"
    payload_path = tmp_path / "payload.nscm"
    payload_path.write_bytes(payload)

    class Cursor:
        def __init__(self):
            self.lastrowid = 7
            self.main_payload = None
            self.chunk_payloads = []
            self._verification = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            if sql.startswith("INSERT INTO `ninjos_schematics`"):
                self.main_payload = params[-1]
            elif sql.startswith("INSERT INTO `ninjos_schematic_payload_chunks`"):
                self.chunk_payloads.append(bytes(params[-1]))
            elif sql.startswith("SELECT COUNT(*)"):
                self._verification = {
                    "count": len(self.chunk_payloads),
                    "bytes": sum(map(len, self.chunk_payloads)),
                }
            return 1

        def fetchone(self):
            return self._verification

    class Connection:
        def __init__(self, cursor):
            self._cursor = cursor
            self.committed = False

        def cursor(self):
            return self._cursor

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("save should not roll back")

    cursor = Cursor()
    connection_obj = Connection(cursor)
    store = object.__new__(MySQLSchematicStore)
    store.table = "ninjos_schematics"
    store.chunk_table = "ninjos_schematic_payload_chunks"
    store.settings = SimpleNamespace(
        inline_payload_max_bytes=4,
        payload_chunk_bytes=4,
        retry_attempts=1,
        retry_backoff_seconds=0.1,
    )

    @contextmanager
    def connection(*, autocommit=True):
        assert autocommit is False
        yield connection_obj

    store._connection = connection
    row = {
        "namespace": "global",
        "name": "large-file",
        "display_name": "Large File",
        "description": "",
        "author_uuid": "u",
        "author_xuid": "x",
        "author_name": "a",
        "source_server": "s",
        "source_dimension": "Overworld",
        "minecraft_version": "1",
        "plugin_version": "1",
        "format_version": 1,
        "size_x": 1,
        "size_y": 1,
        "size_z": 1,
        "block_count": 1,
        "non_air_count": 1,
        "palette_count": 1,
        "includes_air": 1,
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "compressed_bytes": len(payload),
        "uncompressed_bytes": 100,
    }

    original_read_bytes = type(payload_path).read_bytes

    def reject_payload_read_bytes(path):
        if path == payload_path:
            raise AssertionError("chunked save_file must not read the complete payload")
        return original_read_bytes(path)

    monkeypatch.setattr(type(payload_path), "read_bytes", reject_payload_read_bytes)
    receipt = store.save_file(row, payload_path, overwrite=False)
    assert receipt == {"storage": "chunked", "chunk_count": 3, "chunk_bytes": 4}
    assert cursor.main_payload == b""
    assert cursor.chunk_payloads == [b"abcd", b"efgh", b"ij"]
    assert connection_obj.committed is True


def test_store_fetch_to_file_streams_and_validates_chunks(tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from endstone_ninjos_schematics.database import MySQLSchematicStore

    payload = b"abcdefghij"
    rows = [_row(index, piece) for index, piece in enumerate(iter_payload_chunks(payload, 4))]

    class Cursor:
        def __init__(self):
            self.mode = ""
            self.chunk_index = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            if "SELECT * FROM `ninjos_schematics`" in sql:
                self.mode = "main"
            elif "SELECT COUNT(*)" in sql:
                self.mode = "count"
            elif "FROM `ninjos_schematic_payload_chunks`" in sql:
                self.mode = "chunk"
                self.chunk_index = int(params[1])
            return 1

        def fetchone(self):
            if self.mode == "main":
                return {
                    "id": 9,
                    "name": "large",
                    "payload": b"",
                    "compressed_bytes": len(payload),
                    "content_sha256": hashlib.sha256(payload).hexdigest(),
                }
            if self.mode == "count":
                return {"count": len(rows)}
            if self.mode == "chunk":
                return rows[self.chunk_index]
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    store = object.__new__(MySQLSchematicStore)
    store.table = "ninjos_schematics"
    store.chunk_table = "ninjos_schematic_payload_chunks"
    store.settings = SimpleNamespace(namespace="global")

    @contextmanager
    def connection(*, autocommit=True):
        yield Connection()

    store._connection = connection
    destination = tmp_path / "download.nscm"
    result = store.fetch_to_file("Large", destination)
    assert destination.read_bytes() == payload
    assert result["payload_storage"] == "chunked"
    assert result["payload_chunk_count"] == 3
    assert result["payload_path"] == destination
