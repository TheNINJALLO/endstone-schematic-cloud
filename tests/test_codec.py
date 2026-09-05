import unittest

from endstone_ninjos_schematics.codec import (
    RECORD,
    SchematicCodecError,
    append_record,
    decode_schematic,
    encode_schematic,
)


class CodecTests(unittest.TestCase):
    def test_round_trip(self):
        records = bytearray()
        append_record(records, 0, 0, 0, 0)
        append_record(records, 1, 0, 0, 1)
        palette = [
            {"type": "minecraft:stone", "states": {}},
            {"type": "minecraft:oak_stairs", "states": {"weirdo_direction": 0}},
        ]
        encoded = encode_schematic({"size": [2, 1, 1]}, palette, records, 6)
        decoded = decode_schematic(encoded.payload, encoded.sha256_hex)
        self.assertEqual(decoded.size, (2, 1, 1))
        self.assertEqual(decoded.block_count, 2)
        self.assertEqual(decoded.palette, palette)
        self.assertEqual(len(decoded.records), 2 * RECORD.size)

    def test_checksum_rejected(self):
        encoded = encode_schematic({"size": [1, 1, 1]}, [], b"", 6)
        with self.assertRaises(SchematicCodecError):
            decode_schematic(encoded.payload, "0" * 64)


if __name__ == "__main__":
    unittest.main()
