"""Real MariaDB/MySQL checks. Opt in against an isolated local schem_test database.

Set SCHEM_TEST_DB_PORT to the loopback port of a disposable root/no-password
database. Each test owns unique tables and removes only those tables afterwards.
"""

import hashlib
import os
import uuid
from dataclasses import replace

import pytest

from endstone_ninjos_schematics.database import (
    DatabaseError, DatabaseSettings, MySQLSchematicStore, SchematicNotFound,
    normalize_category_name,
)


def test_category_names_and_reserved_uncategorized():
    assert normalize_category_name("Castle Parts") == "castle-parts"
    assert normalize_category_name("Uncategorized") == ""
    assert normalize_category_name("") == ""
    with pytest.raises(ValueError):
        normalize_category_name("///")


@pytest.fixture
def store():
    port = os.environ.get("SCHEM_TEST_DB_PORT")
    if not port:
        pytest.skip("SCHEM_TEST_DB_PORT is required for isolated database integration tests")
    settings = DatabaseSettings(
        "127.0.0.1", int(port), "root", "", "schem_test", "global",
        f"test_{uuid.uuid4().hex[:10]}_", 5, 10, 10, True, "", 32, 64, 1, 0.1,
    )
    result = MySQLSchematicStore(settings)
    result.ensure_schema()
    try:
        yield result
    finally:
        with result._connection() as connection:
            with connection.cursor() as cursor:
                for table in (result.membership_table, result.category_table, result.chunk_table, result.table):
                    cursor.execute(f"DROP TABLE IF EXISTS `{table}`")


def row(store, name="castle", payload=b"original blueprint", category=None):
    result = dict.fromkeys(store._row_columns(), "")
    result.update(
        namespace=store.settings.namespace, name=name, display_name=name,
        format_version=2, size_x=1, size_y=1, size_z=1, block_count=1,
        non_air_count=1, palette_count=1, includes_air=1,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        compressed_bytes=len(payload), uncompressed_bytes=len(payload), payload=payload,
        category=category,
    )
    return result


@pytest.mark.parametrize("payload", [b"inline", b"chunked" * 100], ids=["inline", "chunked"])
def test_category_moves_preserve_payload_and_survive_new_store(store, payload):
    store.save(row(store, payload=payload), overwrite=False)
    assert store.list(category="")[0]["name"] == "castle"
    assert store.create_category("Castle Parts") == "castle-parts"
    store.create_category("castle-parts")  # Idempotent, including concurrent clients.
    store.move("castle", "castle-parts")
    reopened = MySQLSchematicStore(store.settings)
    assert reopened.list(category="") == []
    assert reopened.list(category="castle-parts")[0]["category"] == "castle-parts"
    assert reopened.fetch("castle")["payload"] == payload
    reopened.move("castle", "Uncategorized")
    assert reopened.fetch("castle")["payload"] == payload
    assert reopened.list(category="")[0]["name"] == "castle"


@pytest.mark.parametrize("file_save", [False, True])
def test_category_save_overwrite_and_failed_destination_are_atomic(store, tmp_path, file_save):
    store.create_category("castles")

    def save(payload, category=None, overwrite=False):
        values = row(store, payload=payload, category=category)
        if file_save:
            path = tmp_path / "payload.bin"
            path.write_bytes(payload)
            return store.save_file(values, path, overwrite)
        return store.save(values, overwrite)

    save(b"initial" * 100, "castles")
    save(b"replacement" * 100, overwrite=True)
    assert store.list(category="castles")[0]["name"] == "castle"
    with pytest.raises(DatabaseError, match="does not exist"):
        save(b"must roll back", "missing", overwrite=True)
    assert store.fetch("castle")["payload"] == b"replacement" * 100
    assert store.list(category="castles")[0]["name"] == "castle"
    save(b"final", "", overwrite=True)
    assert store.list(category="")[0]["name"] == "castle"
    with pytest.raises(DatabaseError, match="does not exist"):
        store.move("castle", "missing")
    assert store.list(category="")[0]["name"] == "castle"


def test_categories_are_namespace_scoped_and_paginated(store):
    other = MySQLSchematicStore(replace(store.settings, namespace="other-server"))
    for name in ("alpha", "beta", "gamma"):
        store.create_category(name)
    other.create_category("private")
    store.save(row(store, category="alpha"), False)
    other.save(row(other, category="private"), False)
    assert [entry["name"] for entry in store.list_categories(limit=2)] == ["alpha", "beta"]
    assert [entry["name"] for entry in store.list_categories(limit=2, offset=2)] == ["gamma"]
    with pytest.raises(DatabaseError, match="does not exist"):
        store.move("castle", "private")
    assert other.list()[0]["category"] == "private"
    for number in range(53):
        store.save(row(store, name=f"build-{number:02}", category="alpha"), False)
    first = store.list(search="build", category="alpha", limit=51)
    second = store.list(search="build", category="alpha", limit=51, offset=50)
    assert len(first) == 51 and len(second) == 3
    assert len({entry["name"] for entry in first[:50] + second}) == 53


def test_schema_upgrade_preserves_legacy_saves_and_delete_cleans_membership(store):
    store.save(row(store), False)
    with store._connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE `{store.membership_table}`")
            cursor.execute(f"DROP TABLE `{store.category_table}`")
    store.ensure_schema()
    store.ensure_schema()
    assert store.fetch("castle")["payload"] == b"original blueprint"
    assert store.list(category="")[0]["name"] == "castle"
    store.create_category("castles")
    store.move("castle", "castles")
    store.hard_delete("castle")
    assert store.list(category="castles") == []
    with pytest.raises(SchematicNotFound):
        store.move("castle", "castles")
    with store._connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS count FROM `{store.membership_table}`")
            assert cursor.fetchone()["count"] == 0
