"""Optional bridge to the Endstone BlockData API.

The schematic package deliberately does not declare BlockData as a Python
dependency: the matching native plugin and CPython bridge must come from the
same BlockData release as the running BDS/Endstone build. This module discovers
that installation at runtime and keeps its typed NBT values portable in NSCM.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from importlib import import_module
from typing import Any

ENTITY_SCHEMA_VERSION = 1
_API_SCALAR_MARKER = "__endstone_nbt_scalar__"
_SCALAR_WRAPPER = "__nscm_nbt_scalar__"
_BYTES_WRAPPER = "__nscm_nbt_bytes__"
_FLOAT_WRAPPER = "__nscm_nbt_float__"
_COMPOUND_WRAPPER = "__nscm_nbt_compound__"
_READ_ONLY_NBT_KEYS = {"id", "x", "y", "z", "Items", "items"}
_SCALAR_TYPES = {
    "byte": "_NbtByte",
    "short": "_NbtShort",
    "long": "_NbtLong",
    "float": "_NbtFloat",
}


class BlockDataIntegrationError(RuntimeError):
    """Raised when capture or restoration cannot preserve an entity exactly."""


def _encode_nbt(value: Any, depth: int = 0) -> Any:
    if depth > 64:
        raise BlockDataIntegrationError("BlockData NBT nesting exceeds 64 levels")
    scalar_kind = getattr(type(value), _API_SCALAR_MARKER, None)
    if scalar_kind in _SCALAR_TYPES:
        scalar_value: Any
        if scalar_kind == "float":
            raw_float = float(value)
            scalar_value = (
                raw_float
                if math.isfinite(raw_float)
                else _encode_nbt(raw_float, depth + 1)
            )
        else:
            scalar_value = int(value)
        return {_SCALAR_WRAPPER: scalar_kind, "value": scalar_value}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            label = "nan"
        elif value > 0:
            label = "inf"
        else:
            label = "-inf"
        return {_FLOAT_WRAPPER: label}
    if isinstance(value, (bytes, bytearray)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {_BYTES_WRAPPER: encoded}
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, entry in value.items():
            if not isinstance(key, str):
                raise BlockDataIntegrationError("BlockData NBT compound keys must be strings")
            output[key] = _encode_nbt(entry, depth + 1)
        reserved_shapes = (
            {_SCALAR_WRAPPER, "value"},
            {_BYTES_WRAPPER},
            {_FLOAT_WRAPPER},
            {_COMPOUND_WRAPPER},
        )
        if any(set(output) == shape for shape in reserved_shapes):
            return {_COMPOUND_WRAPPER: output}
        return output
    if isinstance(value, (list, tuple)):
        return [_encode_nbt(entry, depth + 1) for entry in value]
    raise BlockDataIntegrationError(
        f"unsupported BlockData NBT value type: {type(value).__name__}"
    )


def _decode_nbt(value: Any, bridge: Any, depth: int = 0) -> Any:
    if depth > 64:
        raise BlockDataIntegrationError("stored BlockData NBT nesting exceeds 64 levels")
    if isinstance(value, dict):
        if set(value) == {_COMPOUND_WRAPPER} and isinstance(
            value[_COMPOUND_WRAPPER], dict
        ):
            stored = value[_COMPOUND_WRAPPER]
            return {
                key: _decode_nbt(entry, bridge, depth + 1)
                for key, entry in stored.items()
            }
        if set(value) == {_SCALAR_WRAPPER, "value"}:
            kind = value[_SCALAR_WRAPPER]
            class_name = _SCALAR_TYPES.get(kind)
            scalar_type = getattr(bridge, class_name, None) if class_name else None
            if scalar_type is None:
                raise BlockDataIntegrationError(
                    f"the installed BlockData bridge cannot restore typed NBT {kind!r} values"
                )
            return scalar_type(_decode_nbt(value["value"], bridge, depth + 1))
        if set(value) == {_BYTES_WRAPPER}:
            try:
                return base64.b64decode(str(value[_BYTES_WRAPPER]), validate=True)
            except (ValueError, TypeError) as exc:
                raise BlockDataIntegrationError("stored BlockData byte array is invalid") from exc
        if set(value) == {_FLOAT_WRAPPER}:
            names = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}
            try:
                return names[str(value[_FLOAT_WRAPPER])]
            except KeyError as exc:
                raise BlockDataIntegrationError("stored BlockData float marker is invalid") from exc
        return {key: _decode_nbt(entry, bridge, depth + 1) for key, entry in value.items()}
    if isinstance(value, list):
        return [_decode_nbt(entry, bridge, depth + 1) for entry in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise BlockDataIntegrationError("stored BlockData NBT contains an unsupported value")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def snapshot_entity_payload(snapshot: Any) -> dict[str, Any] | None:
    """Convert a captured API snapshot to portable, coordinate-free data."""

    if snapshot is None or str(_field(snapshot, "block_entity_status", "")) != "captured":
        return None
    actor = _field(snapshot, "block_entity")
    if actor is None:
        return None
    raw_nbt = _field(actor, "nbt", {})
    if not isinstance(raw_nbt, dict):
        raise BlockDataIntegrationError("BlockData returned non-compound actor NBT")
    nbt = {
        key: _encode_nbt(value)
        for key, value in raw_nbt.items()
        if key not in _READ_ONLY_NBT_KEYS and not key.startswith("_endstone_")
    }
    inventory = []
    seen_slots: set[int] = set()
    for entry in _field(actor, "inventory", []) or []:
        slot = int(_field(entry, "slot", -1))
        item = _field(entry, "item")
        if slot < 0 or slot in seen_slots or not isinstance(item, dict):
            raise BlockDataIntegrationError("BlockData returned a malformed container inventory")
        seen_slots.add(slot)
        inventory.append([slot, _encode_nbt(item)])
    inventory.sort(key=lambda entry: entry[0])
    return {
        "schema": ENTITY_SCHEMA_VERSION,
        "actor_type": str(_field(actor, "type", "")),
        "canonical_nbt": bool(_field(actor, "canonical_nbt", False)),
        "is_container": bool(_field(actor, "is_container", False)),
        "container_size": max(0, int(_field(actor, "container_size", 0))),
        "nbt": nbt,
        "inventory": inventory,
    }


def validate_entity_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != ENTITY_SCHEMA_VERSION:
        raise BlockDataIntegrationError("stored block entity uses an unsupported schema")
    nbt = payload.get("nbt", {})
    inventory = payload.get("inventory", [])
    if not isinstance(nbt, dict) or not isinstance(inventory, list):
        raise BlockDataIntegrationError("stored block entity is malformed")
    size = payload.get("container_size", 0)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > 4096:
        raise BlockDataIntegrationError("stored block entity has an invalid container size")
    seen: set[int] = set()
    for entry in inventory:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], int)
            or isinstance(entry[0], bool)
            or entry[0] < 0
            or entry[0] >= size
            or entry[0] in seen
            or not isinstance(entry[1], dict)
        ):
            raise BlockDataIntegrationError("stored block entity inventory is malformed")
        seen.add(entry[0])
    return payload


@dataclass(slots=True)
class BlockDataIntegration:
    api: Any
    adapter: Any
    service: Any
    capabilities: dict[str, Any]

    @classmethod
    def connect(cls, server: Any) -> "BlockDataIntegration":
        api = import_module("endstone_blockdata")
        adapter = api.LiveBlockDataAdapter(server)
        if not adapter.available:
            raise BlockDataIntegrationError(
                "the endstone:blockdata:v2 service is not registered"
            )
        capabilities = dict(adapter.capabilities())
        if not capabilities.get("block_entity_nbt"):
            raise BlockDataIntegrationError(
                "the active BlockData adapter cannot capture block-entity NBT"
            )
        return cls(api, adapter, api.BlockDataService(adapter), capabilities)

    @property
    def api_version(self) -> str:
        return str(getattr(self.api, "__version__", "unknown"))

    @property
    def adapter_name(self) -> str:
        return str(self.capabilities.get("adapter", "unknown"))

    def capture(self, dimension: str, position: tuple[int, int, int]) -> dict[str, Any] | None:
        return snapshot_entity_payload(self.service.capture(dimension, position))

    def capture_region(
        self,
        dimension: str,
        minimum: tuple[int, int, int],
        maximum: tuple[int, int, int],
    ) -> dict[tuple[int, int, int], dict[str, Any]]:
        entities: dict[tuple[int, int, int], dict[str, Any]] = {}
        for snapshot in self.service.capture_region(dimension, minimum, maximum):
            payload = snapshot_entity_payload(snapshot)
            if payload is None:
                continue
            location = _field(snapshot, "location")
            position = (
                int(_field(location, "x")),
                int(_field(location, "y")),
                int(_field(location, "z")),
            )
            entities[position] = payload
        return entities

    def restore(
        self,
        dimension: str,
        position: tuple[int, int, int],
        payload: dict[str, Any],
    ) -> None:
        payload = validate_entity_payload(payload)
        bridge = self.adapter.bridge
        location = self.api.BlockLocation(dimension, *position)
        actor_type = str(payload.get("actor_type", ""))
        nbt = _decode_nbt(payload.get("nbt", {}), bridge)
        # The exact adapter deliberately limits Shelf actors to their inventory
        # surface until the actor-specific save/load ABI is available.
        if actor_type in {"minecraft:shelf", "minecraft:chiseled_bookshelf"}:
            nbt = {}
        if nbt:
            if not self.capabilities.get("block_entity_nbt_write"):
                raise BlockDataIntegrationError(
                    "the active BlockData adapter cannot restore block-entity NBT"
                )
            patch = self.api.BlockPatch(location=location, nbt_updates=nbt)
            result = self.service.apply(patch, self.api.ConflictPolicy.FORCE)
            if not result.ok:
                raise BlockDataIntegrationError(
                    f"BlockData NBT restore failed ({result.status}): {result.message}"
                )

        if payload.get("is_container"):
            if not self.capabilities.get("inventory"):
                raise BlockDataIntegrationError(
                    "the active BlockData adapter cannot restore container inventories"
                )
            size = int(payload["container_size"])
            updates = {
                int(slot): _decode_nbt(item, bridge)
                for slot, item in payload.get("inventory", [])
            }
            removals = set(range(size)) - set(updates)
            patch = self.api.BlockPatch(
                location=location,
                inventory_updates=updates,
                inventory_removals=removals,
            )
            result = self.service.apply(patch, self.api.ConflictPolicy.FORCE)
            if not result.ok:
                raise BlockDataIntegrationError(
                    f"BlockData inventory restore failed ({result.status}): {result.message}"
                )
