import unittest

from endstone_ninjos_schematics.rotation import rotate_coord, rotate_states, rotated_size


class RotationTests(unittest.TestCase):
    def test_coordinate_rotation(self):
        size = (3, 2, 5)
        self.assertEqual(rotated_size(size, 90), (5, 2, 3))
        self.assertEqual(rotate_coord(0, 0, 0, size, 90), (4, 0, 0))
        self.assertEqual(rotate_coord(2, 1, 4, size, 180), (0, 1, 0))
        self.assertEqual(rotate_coord(0, 0, 0, size, 270), (0, 0, 2))

    def test_cardinal_state_rotation(self):
        states = {"minecraft:cardinal_direction": "north", "pillar_axis": "x"}
        rotated = rotate_states(states, 90)
        self.assertEqual(rotated["minecraft:cardinal_direction"], "east")
        self.assertEqual(rotated["pillar_axis"], "z")

    def test_facing_direction_rotation(self):
        self.assertEqual(rotate_states({"facing_direction": 2}, 90)["facing_direction"], 5)
        self.assertEqual(rotate_states({"facing_direction": 1}, 90)["facing_direction"], 1)


if __name__ == "__main__":
    unittest.main()
