from contextlib import contextmanager
from types import SimpleNamespace

from endstone_ninjos_schematics.database import MySQLSchematicStore


class Cursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return 1

    def fetchone(self):
        return {"id": 42}


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_hard_delete_removes_chunk_rows_and_main_row_for_current_namespace():
    cursor = Cursor()
    connection_obj = Connection(cursor)
    store = object.__new__(MySQLSchematicStore)
    store.table = "ninjos_schematics"
    store.chunk_table = "ninjos_schematic_payload_chunks"
    store.settings = SimpleNamespace(namespace="global")

    @contextmanager
    def connection(*, autocommit=True):
        assert autocommit is False
        yield connection_obj

    store._connection = connection
    store.hard_delete("Castle Gate")
    assert cursor.calls[0][0].startswith("SELECT `id` FROM `ninjos_schematics`")
    assert cursor.calls[0][1] == ("global", "castle-gate")
    assert cursor.calls[1] == (
        "DELETE FROM `ninjos_schematic_payload_chunks` WHERE `schematic_id`=%s",
        (42,),
    )
    assert cursor.calls[2] == (
        "DELETE FROM `ninjos_schematics` WHERE `id`=%s",
        (42,),
    )
    assert connection_obj.committed is True
