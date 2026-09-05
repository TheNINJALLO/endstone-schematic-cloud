"""Sponge Schematic v3 export for WorldEdit and Amulet.

The native Ninj-OS cloud payload remains the source of truth. This module converts
that palette/record representation into the gzip-compressed big-endian NBT layout
specified by Sponge Schematic v3. The exporter intentionally omits entities,
biomes, and block-entity NBT because Endstone API 0.11 does not expose a stable,
generic serializer for those objects.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from .codec import iter_records
from .models import DecodedSchematic

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

_IDENTIFIER_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
_STATE_KEY_RE = re.compile(r"^[a-z0-9_]+$")


class SpongeSchematicError(ValueError):
    """Raised when a WorldEdit/Amulet export cannot be produced."""


@dataclass(frozen=True, slots=True)
class NBTTag:
    type_id: int
    value: Any
    element_type: int | None = None


def nbt_byte(value: int | bool) -> NBTTag:
    return NBTTag(TAG_BYTE, int(value))


def nbt_short(value: int) -> NBTTag:
    return NBTTag(TAG_SHORT, int(value))


def nbt_int(value: int) -> NBTTag:
    return NBTTag(TAG_INT, int(value))


def nbt_long(value: int) -> NBTTag:
    return NBTTag(TAG_LONG, int(value))


def nbt_string(value: str) -> NBTTag:
    return NBTTag(TAG_STRING, str(value))


def nbt_byte_array(value: bytes | bytearray) -> NBTTag:
    return NBTTag(TAG_BYTE_ARRAY, bytes(value))


def nbt_int_array(value: Iterable[int]) -> NBTTag:
    return NBTTag(TAG_INT_ARRAY, [int(item) for item in value])


def nbt_list(element_type: int, values: Iterable[Any]) -> NBTTag:
    return NBTTag(TAG_LIST, list(values), element_type)


def nbt_compound(value: dict[str, NBTTag]) -> NBTTag:
    return NBTTag(TAG_COMPOUND, value)


def _write_string(stream: BinaryIO, value: str) -> None:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise SpongeSchematicError("NBT string exceeds 65,535 UTF-8 bytes")
    stream.write(struct.pack(">H", len(encoded)))
    stream.write(encoded)


def _signed_short(value: int) -> int:
    if value < 0 or value > 0xFFFF:
        raise SpongeSchematicError("Sponge dimensions must fit an unsigned 16-bit integer")
    return value if value <= 0x7FFF else value - 0x10000


def _write_payload(stream: BinaryIO, tag: NBTTag) -> None:
    type_id = tag.type_id
    value = tag.value
    if type_id == TAG_BYTE:
        stream.write(struct.pack(">b", int(value)))
    elif type_id == TAG_SHORT:
        stream.write(struct.pack(">h", int(value)))
    elif type_id == TAG_INT:
        stream.write(struct.pack(">i", int(value)))
    elif type_id == TAG_LONG:
        stream.write(struct.pack(">q", int(value)))
    elif type_id == TAG_FLOAT:
        stream.write(struct.pack(">f", float(value)))
    elif type_id == TAG_DOUBLE:
        stream.write(struct.pack(">d", float(value)))
    elif type_id == TAG_BYTE_ARRAY:
        raw = bytes(value)
        stream.write(struct.pack(">i", len(raw)))
        stream.write(raw)
    elif type_id == TAG_STRING:
        _write_string(stream, str(value))
    elif type_id == TAG_LIST:
        element_type = int(tag.element_type if tag.element_type is not None else TAG_END)
        values = list(value)
        stream.write(struct.pack(">bi", element_type, len(values)))
        for item in values:
            child = item if isinstance(item, NBTTag) else NBTTag(element_type, item)
            if child.type_id != element_type:
                raise SpongeSchematicError("NBT list contains mixed tag types")
            _write_payload(stream, child)
    elif type_id == TAG_COMPOUND:
        for name, child in value.items():
            stream.write(struct.pack(">b", child.type_id))
            _write_string(stream, name)
            _write_payload(stream, child)
        stream.write(b"\x00")
    elif type_id == TAG_INT_ARRAY:
        values = [int(item) for item in value]
        stream.write(struct.pack(">i", len(values)))
        for item in values:
            stream.write(struct.pack(">i", item))
    elif type_id == TAG_LONG_ARRAY:
        values = [int(item) for item in value]
        stream.write(struct.pack(">i", len(values)))
        for item in values:
            stream.write(struct.pack(">q", item))
    else:
        raise SpongeSchematicError(f"unsupported NBT tag type {type_id}")


def encode_named_nbt(root_name: str, root: NBTTag) -> bytes:
    if root.type_id == TAG_END:
        raise SpongeSchematicError("root NBT tag cannot be TAG_End")
    stream = io.BytesIO()
    stream.write(struct.pack(">b", root.type_id))
    _write_string(stream, root_name)
    _write_payload(stream, root)
    return stream.getvalue()


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise SpongeSchematicError("palette indexes must be non-negative")
    output = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            part |= 0x80
        output.append(part)
        if not value:
            return bytes(output)


_FACING_DIRECTION = {
    0: "down",
    1: "up",
    2: "north",
    3: "south",
    4: "west",
    5: "east",
}
_DIRECTION = {0: "south", 1: "west", 2: "north", 3: "east"}
_WEIRDO_DIRECTION = {0: "east", 1: "west", 2: "south", 3: "north"}
_RAIL_DIRECTION = {
    0: "north_south",
    1: "east_west",
    2: "ascending_east",
    3: "ascending_west",
    4: "ascending_north",
    5: "ascending_south",
    6: "south_east",
    7: "south_west",
    8: "north_west",
    9: "north_east",
}

# Properties whose names and value shapes are shared by modern Java blockstates.
_DIRECT_JAVA_PROPERTIES = {
    "age",
    "axis",
    "bites",
    "bottom",
    "candles",
    "charges",
    "conditional",
    "delay",
    "disarmed",
    "distance",
    "down",
    "east",
    "eggs",
    "enabled",
    "extended",
    "eye",
    "face",
    "facing",
    "half",
    "hanging",
    "has_book",
    "hatch",
    "hinge",
    "in_wall",
    "layers",
    "leaves",
    "level",
    "lit",
    "locked",
    "mode",
    "moisture",
    "north",
    "note",
    "occupied",
    "open",
    "orientation",
    "part",
    "persistent",
    "pickles",
    "power",
    "powered",
    "rotation",
    "shape",
    "short",
    "signal_fire",
    "snowy",
    "south",
    "stage",
    "triggered",
    "type",
    "unstable",
    "up",
    "waterlogged",
    "west",
}

_ID_REMAP = {
    "minecraft:grass": "minecraft:grass_block",
    "minecraft:grass_path": "minecraft:dirt_path",
    "minecraft:stonebrick": "minecraft:stone_bricks",
    "minecraft:wooden_door": "minecraft:oak_door",
    "minecraft:wooden_button": "minecraft:oak_button",
    "minecraft:wooden_pressure_plate": "minecraft:oak_pressure_plate",
    "minecraft:standing_sign": "minecraft:oak_sign",
    "minecraft:wall_sign": "minecraft:oak_wall_sign",
    "minecraft:lit_furnace": "minecraft:furnace",
    "minecraft:lit_redstone_ore": "minecraft:redstone_ore",
    "minecraft:unlit_redstone_torch": "minecraft:redstone_torch",
}


@dataclass(slots=True)
class ConversionReport:
    source_palette_count: int = 0
    output_palette_count: int = 0
    remapped_identifiers: dict[str, str] = field(default_factory=dict)
    stripped_states: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "Sponge Schematic v3",
            "source_palette_count": self.source_palette_count,
            "output_palette_count": self.output_palette_count,
            "remapped_identifiers": dict(sorted(self.remapped_identifiers.items())),
            "stripped_states": {key: sorted(value) for key, value in sorted(self.stripped_states.items())},
            "warnings": list(self.warnings),
        }


def _java_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.lower()
    return str(value).lower()


def _map_identifier(block_type: str, states: dict[str, Any], report: ConversionReport) -> str:
    original = block_type.lower()
    mapped = _ID_REMAP.get(original, original)

    # A few high-value legacy Bedrock palette families are flattened into Java IDs.
    color = states.get("color")
    if isinstance(color, str):
        color_name = color.lower().replace(" ", "_")
        families = {
            "minecraft:wool": "wool",
            "minecraft:concrete": "concrete",
            "minecraft:concrete_powder": "concrete_powder",
            "minecraft:stained_hardened_clay": "terracotta",
            "minecraft:stained_glass": "stained_glass",
            "minecraft:stained_glass_pane": "stained_glass_pane",
            "minecraft:carpet": "carpet",
        }
        suffix = families.get(original)
        if suffix:
            mapped = f"minecraft:{color_name}_{suffix}"

    wood_type = states.get("wood_type")
    if original == "minecraft:planks" and isinstance(wood_type, str):
        mapped = f"minecraft:{wood_type.lower()}_planks"

    if mapped != original:
        report.remapped_identifiers[original] = mapped
    if not _IDENTIFIER_RE.fullmatch(mapped):
        report.warnings.append(f"Invalid resource identifier '{block_type}' was replaced with minecraft:air")
        return "minecraft:air"
    return mapped


def _map_states(block_type: str, states: dict[str, Any], report: ConversionReport) -> dict[str, str]:
    mapped: dict[str, str] = {}
    consumed: set[str] = set()

    def add(key: str, value: Any) -> None:
        if _STATE_KEY_RE.fullmatch(key):
            mapped[key] = _java_value(value)

    if "facing_direction" in states:
        consumed.add("facing_direction")
        try:
            add("facing", _FACING_DIRECTION[int(states["facing_direction"])])
        except (KeyError, TypeError, ValueError):
            pass
    elif "weirdo_direction" in states:
        consumed.add("weirdo_direction")
        try:
            add("facing", _WEIRDO_DIRECTION[int(states["weirdo_direction"])])
        except (KeyError, TypeError, ValueError):
            pass
    elif "direction" in states:
        consumed.add("direction")
        try:
            add("facing", _DIRECTION[int(states["direction"])])
        except (KeyError, TypeError, ValueError):
            pass

    if "pillar_axis" in states:
        consumed.add("pillar_axis")
        add("axis", states["pillar_axis"])
    if "ground_sign_direction" in states:
        consumed.add("ground_sign_direction")
        add("rotation", states["ground_sign_direction"])
    if "rail_direction" in states:
        consumed.add("rail_direction")
        try:
            add("shape", _RAIL_DIRECTION[int(states["rail_direction"])])
        except (KeyError, TypeError, ValueError):
            pass
    if "vertical_half" in states:
        consumed.add("vertical_half")
        add("half", states["vertical_half"])
    if "upside_down_bit" in states:
        consumed.add("upside_down_bit")
        add("half", "top" if bool(states["upside_down_bit"]) else "bottom")
    if "upper_block_bit" in states:
        consumed.add("upper_block_bit")
        add("half", "upper" if bool(states["upper_block_bit"]) else "lower")
    if "door_hinge_bit" in states:
        consumed.add("door_hinge_bit")
        add("hinge", "right" if bool(states["door_hinge_bit"]) else "left")
    if "head_piece_bit" in states:
        consumed.add("head_piece_bit")
        add("part", "head" if bool(states["head_piece_bit"]) else "foot")

    bit_aliases = {
        "open_bit": "open",
        "powered_bit": "powered",
        "occupied_bit": "occupied",
        "persistent_bit": "persistent",
        "in_wall_bit": "in_wall",
        "attached_bit": "attached",
    }
    for source, target in bit_aliases.items():
        if source in states:
            consumed.add(source)
            add(target, bool(states[source]))

    # Preserve already-Java-shaped properties. For custom namespaces, preserve
    # every syntactically safe property so modded Java environments have a chance
    # to understand matching custom blocks.
    custom_namespace = not block_type.startswith("minecraft:")
    for key, value in states.items():
        normalized = str(key).lower()
        if normalized in consumed or normalized in {"color", "wood_type"}:
            continue
        if normalized in _DIRECT_JAVA_PROPERTIES or custom_namespace:
            add(normalized, value)
        else:
            report.stripped_states.setdefault(block_type, []).append(normalized)
    return mapped


def java_blockstate_string(entry: dict[str, Any], report: ConversionReport) -> str:
    block_type = str(entry.get("type", "minecraft:air")).lower()
    states = dict(entry.get("states", {}))
    mapped_type = _map_identifier(block_type, states, report)
    mapped_states = _map_states(block_type, states, report)
    if not mapped_states:
        return mapped_type
    properties = ",".join(f"{key}={value}" for key, value in sorted(mapped_states.items()))
    return f"{mapped_type}[{properties}]"


def _palette_and_data(
    schematic: DecodedSchematic,
    report: ConversionReport,
) -> tuple[dict[str, int], bytes]:
    width, height, length = schematic.size
    volume = width * height * length
    if volume <= 0:
        raise SpongeSchematicError("schematic dimensions must be positive")
    if any(axis > 0xFFFF for axis in (width, height, length)):
        raise SpongeSchematicError("Sponge v3 supports dimensions up to 65,535 per axis")

    report.source_palette_count = len(schematic.palette)
    source_to_java: list[str] = [java_blockstate_string(entry, report) for entry in schematic.palette]
    palette: dict[str, int] = {"minecraft:air": 0}
    source_to_output: list[int] = []
    for blockstate in source_to_java:
        index = palette.get(blockstate)
        if index is None:
            index = len(palette)
            palette[blockstate] = index
        source_to_output.append(index)

    indexes = [palette["minecraft:air"]] * volume
    for dx, dy, dz, source_palette_index in iter_records(schematic.records):
        if dx >= width or dy >= height or dz >= length:
            raise SpongeSchematicError(
                f"native record {dx},{dy},{dz} lies outside {width}×{height}×{length}"
            )
        if source_palette_index >= len(source_to_output):
            raise SpongeSchematicError("native record references an invalid palette index")
        linear = int(dx) + int(dz) * width + int(dy) * width * length
        indexes[linear] = source_to_output[source_palette_index]

    report.output_palette_count = len(palette)
    if not bool(schematic.header.get("includes_air", True)):
        report.warnings.append(
            "The native schematic excluded air. Sponge requires a complete volume, so missing positions were exported as air."
        )
    data = bytearray()
    for index in indexes:
        data.extend(encode_varint(index))
    return palette, bytes(data)


def encode_sponge_v3(
    schematic: DecodedSchematic,
    *,
    data_version: int = 4671,
    name: str | None = None,
    author: str | None = None,
    plugin_version: str = "",
    compression_level: int = 6,
) -> tuple[bytes, ConversionReport]:
    """Convert a decoded native schematic to gzip-compressed Sponge v3 NBT."""

    width, height, length = schematic.size
    report = ConversionReport()
    palette, block_data = _palette_and_data(schematic, report)
    header = schematic.header
    metadata = {
        "Name": nbt_string(name or str(header.get("display_name") or header.get("name") or "schematic")),
        "Author": nbt_string(author or str(header.get("author_name") or "Unknown")),
        "Date": nbt_long(int(time.time() * 1000)),
        "NinjOSSourceEdition": nbt_string("Bedrock"),
        "NinjOSSourceVersion": nbt_string(str(header.get("minecraft_version", "unknown"))),
        "NinjOSSourceServer": nbt_string(str(header.get("source_server", "unknown"))),
        "NinjOSPluginVersion": nbt_string(plugin_version or str(header.get("plugin_version", "unknown"))),
    }
    blocks = {
        "Palette": nbt_compound({state: nbt_int(index) for state, index in palette.items()}),
        "Data": nbt_byte_array(block_data),
        # Including an empty list maximizes compatibility with WorldEdit readers.
        "BlockEntities": nbt_list(TAG_COMPOUND, []),
    }
    schematic_compound = {
        "Version": nbt_int(3),
        "DataVersion": nbt_int(max(0, int(data_version))),
        "Metadata": nbt_compound(metadata),
        "Width": nbt_short(_signed_short(width)),
        "Height": nbt_short(_signed_short(height)),
        "Length": nbt_short(_signed_short(length)),
        "Offset": nbt_int_array([0, 0, 0]),
        "Blocks": nbt_compound(blocks),
        "Entities": nbt_list(TAG_COMPOUND, []),
    }
    raw = encode_named_nbt("", nbt_compound({"Schematic": nbt_compound(schematic_compound)}))
    output = io.BytesIO()
    level = max(0, min(9, int(compression_level)))
    with gzip.GzipFile(fileobj=output, mode="wb", compresslevel=level, mtime=0) as archive:
        archive.write(raw)
    return output.getvalue(), report


# Minimal NBT reader used by validation tests and disk verification. It supports
# every standard tag type, so exported files can be checked without a third-party
# dependency on the Endstone host.
def _read_exact(stream: BinaryIO, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise SpongeSchematicError("NBT stream is truncated")
    return data


def _read_string(stream: BinaryIO) -> str:
    length = struct.unpack(">H", _read_exact(stream, 2))[0]
    try:
        return _read_exact(stream, length).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpongeSchematicError(f"invalid NBT UTF-8 string: {exc}") from exc


def _read_payload(stream: BinaryIO, type_id: int) -> Any:
    if type_id == TAG_BYTE:
        return struct.unpack(">b", _read_exact(stream, 1))[0]
    if type_id == TAG_SHORT:
        return struct.unpack(">h", _read_exact(stream, 2))[0]
    if type_id == TAG_INT:
        return struct.unpack(">i", _read_exact(stream, 4))[0]
    if type_id == TAG_LONG:
        return struct.unpack(">q", _read_exact(stream, 8))[0]
    if type_id == TAG_FLOAT:
        return struct.unpack(">f", _read_exact(stream, 4))[0]
    if type_id == TAG_DOUBLE:
        return struct.unpack(">d", _read_exact(stream, 8))[0]
    if type_id == TAG_BYTE_ARRAY:
        length = struct.unpack(">i", _read_exact(stream, 4))[0]
        if length < 0:
            raise SpongeSchematicError("negative NBT byte-array length")
        return _read_exact(stream, length)
    if type_id == TAG_STRING:
        return _read_string(stream)
    if type_id == TAG_LIST:
        element_type, length = struct.unpack(">bi", _read_exact(stream, 5))
        if length < 0:
            raise SpongeSchematicError("negative NBT list length")
        return [_read_payload(stream, element_type) for _ in range(length)]
    if type_id == TAG_COMPOUND:
        compound: dict[str, Any] = {}
        while True:
            child_type = struct.unpack(">b", _read_exact(stream, 1))[0]
            if child_type == TAG_END:
                return compound
            child_name = _read_string(stream)
            compound[child_name] = _read_payload(stream, child_type)
    if type_id == TAG_INT_ARRAY:
        length = struct.unpack(">i", _read_exact(stream, 4))[0]
        if length < 0:
            raise SpongeSchematicError("negative NBT int-array length")
        return [struct.unpack(">i", _read_exact(stream, 4))[0] for _ in range(length)]
    if type_id == TAG_LONG_ARRAY:
        length = struct.unpack(">i", _read_exact(stream, 4))[0]
        if length < 0:
            raise SpongeSchematicError("negative NBT long-array length")
        return [struct.unpack(">q", _read_exact(stream, 8))[0] for _ in range(length)]
    raise SpongeSchematicError(f"unsupported NBT tag type {type_id}")


def decode_named_nbt(data: bytes, *, compressed: bool = True) -> tuple[str, Any]:
    raw = gzip.decompress(data) if compressed else data
    stream = io.BytesIO(raw)
    root_type = struct.unpack(">b", _read_exact(stream, 1))[0]
    if root_type == TAG_END:
        raise SpongeSchematicError("NBT root cannot be TAG_End")
    root_name = _read_string(stream)
    payload = _read_payload(stream, root_type)
    if stream.read(1):
        raise SpongeSchematicError("trailing bytes after NBT root")
    return root_name, payload


@dataclass(frozen=True, slots=True)
class WorldEditSettings:
    enabled: bool
    directory: Path
    data_version: int
    overwrite_exports: bool
    write_conversion_report: bool
    max_file_bytes: int

    @classmethod
    def from_config(cls, config: dict[str, Any], data_folder: str | Path) -> "WorldEditSettings":
        section = config.get("worldedit", {})
        directory = Path(str(section.get("directory", "worldedit_schematics")).strip() or "worldedit_schematics").expanduser()
        if not directory.is_absolute():
            directory = Path(data_folder) / directory
        max_mb = max(1, int(section.get("max_file_size_mb", 1024)))
        return cls(
            enabled=bool(section.get("enabled", True)),
            directory=directory.resolve(strict=False),
            data_version=max(0, int(section.get("java_data_version", 4671))),
            overwrite_exports=bool(section.get("overwrite_exports", True)),
            write_conversion_report=bool(section.get("write_conversion_report", True)),
            max_file_bytes=max_mb * 1024 * 1024,
        )


class WorldEditSchematicStore:
    def __init__(self, settings: WorldEditSettings):
        self.settings = settings
        if settings.enabled:
            self.ensure_directory()

    @property
    def root(self) -> Path:
        return self.settings.directory

    def ensure_directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise SpongeSchematicError(f"WorldEdit export path is not a directory: {self.root}")

    @staticmethod
    def _safe_name(name: str) -> str:
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", name.strip().lower()).strip("-.")
        if not normalized:
            raise SpongeSchematicError("WorldEdit schematic name is empty after normalization")
        return normalized[:96]

    def path(self, name: str) -> Path:
        return self.root / f"{self._safe_name(name)}.schem"

    def report_path(self, name: str) -> Path:
        return self.root / f"{self._safe_name(name)}.schem.conversion.json"

    def save(
        self,
        name: str,
        payload: bytes,
        report: ConversionReport,
        *,
        overwrite: bool | None = None,
    ) -> Path:
        if not self.settings.enabled:
            raise SpongeSchematicError("WorldEdit/Amulet export is disabled in config.toml")
        if len(payload) > self.settings.max_file_bytes:
            raise SpongeSchematicError(
                f"WorldEdit schematic is {len(payload):,} bytes; configured maximum is {self.settings.max_file_bytes:,}"
            )
        self.ensure_directory()
        destination = self.path(name)
        allow_overwrite = self.settings.overwrite_exports if overwrite is None else bool(overwrite)
        if destination.exists() and not allow_overwrite:
            raise SpongeSchematicError(f"WorldEdit schematic '{destination.name}' already exists")

        # Parse the final bytes before replacing the destination. This catches
        # malformed NBT or gzip output before an old good export is overwritten.
        _, root = decode_named_nbt(payload)
        if not isinstance(root, dict) or "Schematic" not in root:
            raise SpongeSchematicError("generated file did not contain a Sponge Schematic root")

        temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        report_temp: Path | None = None
        try:
            with temp.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)

            if self.settings.write_conversion_report:
                report_destination = self.report_path(name)
                report_temp = report_destination.with_name(
                    f".{report_destination.name}.{os.getpid()}.tmp"
                )
                with report_temp.open("w", encoding="utf-8") as stream:
                    json.dump(report.as_dict(), stream, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(report_temp, report_destination)
        finally:
            temp.unlink(missing_ok=True)
            if report_temp is not None:
                report_temp.unlink(missing_ok=True)
        return destination
