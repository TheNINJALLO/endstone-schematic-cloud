import unittest

from endstone_ninjos_schematics.codec import append_record, iter_records
from endstone_ninjos_schematics.models import BlockPos, DecodedSchematic
from endstone_ninjos_schematics.planner import build_chunk_regions, prepare_paste_plan


class PlannerTests(unittest.TestCase):
    def test_chunk_regions_cover_selection_once(self):
        low = BlockPos(-2, 10, 14)
        size = (20, 3, 20)
        regions = build_chunk_regions(low, size)
        self.assertEqual(sum(region.volume for region in regions), 20 * 3 * 20)
        self.assertGreater(len(regions), 1)
        self.assertTrue(all(region.min_x // 16 == region.chunk_x for region in regions))
        self.assertTrue(all(region.min_z // 16 == region.chunk_z for region in regions))
        covered = {
            (x, y, z)
            for region in regions
            for y in range(region.min_y, region.max_y + 1)
            for z in range(region.min_z, region.max_z + 1)
            for x in range(region.min_x, region.max_x + 1)
        }
        expected = {
            (x, y, z)
            for y in range(low.y, low.y + size[1])
            for z in range(low.z, low.z + size[2])
            for x in range(low.x, low.x + size[0])
        }
        self.assertEqual(covered, expected)

    def test_paste_plan_rotates_and_groups_by_target_chunk(self):
        records = bytearray()
        append_record(records, 0, 0, 0, 0)
        append_record(records, 19, 0, 0, 0)
        schematic = DecodedSchematic(
            header={"size": [20, 1, 2], "block_count": 2},
            palette=[{"type": "minecraft:oak_stairs", "states": {"weirdo_direction": 3}}],
            records=bytes(records),
        )
        plan = prepare_paste_plan(schematic, BlockPos(15, 64, 15), 90)
        self.assertEqual(plan.size, (2, 1, 20))
        self.assertEqual(plan.block_count, 2)
        self.assertGreaterEqual(len(plan.chunks), 2)
        planned = list(iter_records(plan.records))
        self.assertCountEqual(
            [(x, y, z) for x, y, z, _ in planned],
            [(1, 0, 0), (1, 0, 19)],
        )
        self.assertEqual(plan.palette[0]["states"]["weirdo_direction"], 0)


if __name__ == "__main__":
    unittest.main()


def test_integrity_rejects_incomplete_full_volume_schematic():
    from endstone_ninjos_schematics.planner import validate_schematic_integrity

    records = bytearray()
    append_record(records, 0, 0, 0, 0)
    schematic = DecodedSchematic(
        header={
            "size": [2, 1, 1],
            "block_count": 1,
            "selection_volume": 2,
            "includes_air": True,
        },
        palette=[{"type": "minecraft:stone", "states": {}}],
        records=bytes(records),
    )
    with unittest.TestCase().assertRaisesRegex(ValueError, "full-volume schematic is incomplete"):
        validate_schematic_integrity(schematic)
