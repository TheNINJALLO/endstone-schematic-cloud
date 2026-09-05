from pathlib import Path

from endstone_ninjos_schematics.codec import append_record
from endstone_ninjos_schematics.models import DecodedSchematic
from endstone_ninjos_schematics.sponge_schem import (
    WorldEditSchematicStore,
    WorldEditSettings,
    decode_named_nbt,
    encode_sponge_v3,
    java_blockstate_string,
    ConversionReport,
)


def make_schematic(*, includes_air: bool = False) -> DecodedSchematic:
    records = bytearray()
    # Sparse native schematics are legal. Sponge output must fill the remainder.
    append_record(records, 0, 0, 0, 0)
    append_record(records, 1, 1, 1, 1)
    return DecodedSchematic(
        header={
            "name": "test-house",
            "display_name": "Test House",
            "author_name": "Builder",
            "source_server": "unit-test",
            "minecraft_version": "26.30",
            "plugin_version": "1.3.0",
            "size": [2, 2, 2],
            "block_count": 2,
            "includes_air": includes_air,
        },
        palette=[
            {
                "type": "minecraft:oak_stairs",
                "states": {"weirdo_direction": 2, "upside_down_bit": True},
            },
            {"type": "minecraft:grass", "states": {"snowy": False}},
        ],
        records=bytes(records),
    )


def test_java_blockstate_mapping():
    report = ConversionReport()
    state = java_blockstate_string(
        {
            "type": "minecraft:oak_stairs",
            "states": {"weirdo_direction": 2, "upside_down_bit": True},
        },
        report,
    )
    assert state == "minecraft:oak_stairs[facing=south,half=top]"


def test_encode_sponge_v3_structure_and_palette():
    payload, report = encode_sponge_v3(
        make_schematic(), data_version=4671, plugin_version="1.3.0"
    )
    root_name, root = decode_named_nbt(payload)
    assert root_name == ""
    schematic = root["Schematic"]
    assert schematic["Version"] == 3
    assert schematic["DataVersion"] == 4671
    assert schematic["Width"] == 2
    assert schematic["Height"] == 2
    assert schematic["Length"] == 2
    assert schematic["Offset"] == [0, 0, 0]
    assert schematic["Metadata"]["Name"] == "Test House"
    assert schematic["Metadata"]["NinjOSSourceEdition"] == "Bedrock"
    palette = schematic["Blocks"]["Palette"]
    assert palette["minecraft:air"] == 0
    assert "minecraft:oak_stairs[facing=south,half=top]" in palette
    assert "minecraft:grass_block[snowy=false]" in palette
    # Eight entries are required for a 2x2x2 volume, all indexes are one-byte varints.
    assert len(schematic["Blocks"]["Data"]) == 8
    assert schematic["Blocks"]["BlockEntities"] == []
    assert schematic["Entities"] == []
    assert report.output_palette_count == 3
    assert report.warnings


def test_worldedit_store_writes_schem_and_report(tmp_path: Path):
    settings = WorldEditSettings(
        enabled=True,
        directory=tmp_path / "we",
        data_version=4671,
        overwrite_exports=False,
        write_conversion_report=True,
        max_file_bytes=1024 * 1024,
    )
    store = WorldEditSchematicStore(settings)
    payload, report = encode_sponge_v3(make_schematic(), data_version=4671)
    destination = store.save("My Test House", payload, report)
    assert destination.name == "my-test-house.schem"
    assert destination.is_file()
    assert store.report_path("My Test House").is_file()
    _, root = decode_named_nbt(destination.read_bytes())
    assert root["Schematic"]["Version"] == 3
