from types import SimpleNamespace

from endstone_ninjos_schematics.blockdata_integration import (
    BlockDataIntegration,
    snapshot_entity_payload,
)


class NbtByte(int):
    __endstone_nbt_scalar__ = "byte"


class NbtShort(int):
    __endstone_nbt_scalar__ = "short"


class NbtLong(int):
    __endstone_nbt_scalar__ = "long"


class NbtFloat(float):
    __endstone_nbt_scalar__ = "float"


class FakeBlockLocation:
    def __init__(self, dimension, x, y, z):
        self.dimension = dimension
        self.x = x
        self.y = y
        self.z = z


class FakeBlockPatch:
    def __init__(self, **values):
        self.__dict__.update(values)
        self.nbt_updates = values.get("nbt_updates", {})
        self.inventory_updates = values.get("inventory_updates", {})
        self.inventory_removals = values.get("inventory_removals", set())


class FakeService:
    def __init__(self):
        self.patches = []

    def apply(self, patch, policy):
        self.patches.append((patch, policy))
        return SimpleNamespace(ok=True, status="applied", message="applied")


def _snapshot():
    actor = SimpleNamespace(
        type="minecraft:chest",
        nbt={
            "id": "Chest",
            "x": 100,
            "y": 64,
            "z": 200,
            "_endstone_bds_build": "1.26.45",
            "CustomName": "Vault",
            "BurnTime": NbtShort(12),
            "Collision": {"__nscm_nbt_scalar__": "byte", "value": 99},
            "Items": [],
        },
        raw_snbt="",
        canonical_nbt=True,
        is_container=True,
        container_size=3,
        inventory=[
            SimpleNamespace(
                slot=1,
                item={"Name": "minecraft:diamond", "Count": NbtByte(4)},
            )
        ],
    )
    return SimpleNamespace(block_entity_status="captured", block_entity=actor)


def test_snapshot_payload_is_coordinate_free_and_preserves_typed_nbt():
    payload = snapshot_entity_payload(_snapshot())
    assert payload is not None
    assert payload["nbt"]["CustomName"] == "Vault"
    assert payload["nbt"]["BurnTime"] == {
        "__nscm_nbt_scalar__": "short",
        "value": 12,
    }
    assert payload["nbt"]["Collision"] == {
        "__nscm_nbt_compound__": {
            "__nscm_nbt_scalar__": "byte",
            "value": 99,
        }
    }
    assert not ({"id", "x", "y", "z", "Items"} & payload["nbt"].keys())
    assert payload["inventory"][0][1]["Count"] == {
        "__nscm_nbt_scalar__": "byte",
        "value": 4,
    }


def test_restore_splits_actor_nbt_and_inventory_and_clears_empty_slots():
    bridge = SimpleNamespace(
        _NbtByte=NbtByte,
        _NbtShort=NbtShort,
        _NbtLong=NbtLong,
        _NbtFloat=NbtFloat,
    )
    api = SimpleNamespace(
        BlockLocation=FakeBlockLocation,
        BlockPatch=FakeBlockPatch,
        ConflictPolicy=SimpleNamespace(FORCE="force"),
    )
    service = FakeService()
    integration = BlockDataIntegration(
        api=api,
        adapter=SimpleNamespace(bridge=bridge),
        service=service,
        capabilities={"block_entity_nbt_write": True, "inventory": True},
    )
    payload = snapshot_entity_payload(_snapshot())
    assert payload is not None

    integration.restore("Overworld", (5, 70, 9), payload)

    assert len(service.patches) == 2
    nbt_patch, nbt_policy = service.patches[0]
    assert nbt_policy == "force"
    assert nbt_patch.nbt_updates["CustomName"] == "Vault"
    assert type(nbt_patch.nbt_updates["BurnTime"]) is NbtShort
    assert nbt_patch.nbt_updates["Collision"] == {
        "__nscm_nbt_scalar__": "byte",
        "value": 99,
    }
    inventory_patch, inventory_policy = service.patches[1]
    assert inventory_policy == "force"
    assert type(inventory_patch.inventory_updates[1]["Count"]) is NbtByte
    assert inventory_patch.inventory_removals == {0, 2}
