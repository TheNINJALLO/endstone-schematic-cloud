from pathlib import Path

import pytest

from endstone_ninjos_schematics.codec import append_record, encode_schematic
from endstone_ninjos_schematics.disk_store import (
    DiskSchematicExists,
    DiskSchematicStore,
    DiskSettings,
)


def _row():
    records = bytearray()
    append_record(records, 0, 0, 0, 0)
    encoded = encode_schematic(
        {
            "name": "tower",
            "display_name": "Tower",
            "description": "Stone tower",
            "author_name": "Architect",
            "source_server": "test",
            "source_dimension": "Overworld",
            "size": [1, 1, 1],
        },
        [{"type": "minecraft:stone", "states": {}}],
        records,
    )
    return {
        "name": "tower",
        "display_name": "Tower",
        "description": "Stone tower",
        "author_name": "Architect",
        "source_server": "test",
        "size_x": 1,
        "size_y": 1,
        "size_z": 1,
        "block_count": 1,
        "content_sha256": encoded.sha256_hex,
        "payload": encoded.payload,
    }


def _store(tmp_path: Path, overwrite=True):
    settings = DiskSettings(
        enabled=True,
        directory=tmp_path / "backups",
        extension=".schem",
        auto_create_directory=True,
        write_metadata_sidecar=True,
        overwrite_cloud_exports=overwrite,
        max_file_bytes=1024 * 1024,
    )
    return DiskSchematicStore(settings)


def test_cloud_row_is_saved_as_verified_schem_with_metadata(tmp_path):
    store = _store(tmp_path)
    path = store.save_cloud_row(_row())
    assert path.name == "tower.schem"
    assert path.is_file()
    assert (path.parent / "tower.schem.json").is_file()
    payload, metadata = store.read("tower")
    assert payload == _row()["payload"]
    assert metadata["block_count"] == 1
    assert metadata["display_name"] == "Tower"


def test_existing_disk_copy_is_protected_when_overwrite_is_false(tmp_path):
    store = _store(tmp_path, overwrite=False)
    store.save_cloud_row(_row())
    with pytest.raises(DiskSchematicExists):
        store.save_cloud_row(_row())


def test_relative_directory_is_resolved_below_plugin_data_folder(tmp_path):
    settings = DiskSettings.from_config(
        {"disk": {"directory": "schematics"}}, tmp_path / "plugin-data"
    )
    assert settings.directory == (tmp_path / "plugin-data" / "schematics").resolve()


def test_save_cloud_file_copies_payload_without_whole_file_read(tmp_path, monkeypatch):
    import hashlib
    import json

    from endstone_ninjos_schematics.disk_store import DiskSettings, DiskSchematicStore

    root = tmp_path / "disk"
    settings = DiskSettings(
        enabled=True,
        directory=root,
        extension=".nscm",
        auto_create_directory=True,
        write_metadata_sidecar=True,
        overwrite_cloud_exports=True,
        max_file_bytes=1024 * 1024,
    )
    store = DiskSchematicStore(settings)
    payload = b"stream-me" * 1000
    source = tmp_path / "source.nscm"
    source.write_bytes(payload)
    row = {
        "name": "Castle",
        "display_name": "Castle",
        "compressed_bytes": len(payload),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "size_x": 10,
        "size_y": 20,
        "size_z": 30,
        "block_count": 6000,
    }

    original_read_bytes = type(source).read_bytes

    def reject_source_read_bytes(path):
        if path == source:
            raise AssertionError("save_cloud_file must stream the source")
        return original_read_bytes(path)

    monkeypatch.setattr(type(source), "read_bytes", reject_source_read_bytes)
    destination = store.save_cloud_file(row, source)
    with destination.open("rb") as stream:
        assert stream.read() == payload
    metadata = json.loads(store.metadata_path("castle").read_text(encoding="utf-8"))
    assert metadata["size"] == [10, 20, 30]
    assert metadata["block_count"] == 6000
