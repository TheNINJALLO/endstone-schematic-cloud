import json
import unittest
import zlib

from endstone_ninjos_schematics.codec import (
    RECORD,
    MAGIC,
    PREFIX,
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

    def test_block_entities_round_trip_in_v2_sidecar(self):
        records = bytearray()
        append_record(records, 2, 0, 1, 0)
        entity = {
            "schema": 1,
            "actor_type": "minecraft:chest",
            "canonical_nbt": True,
            "is_container": True,
            "container_size": 27,
            "nbt": {"CustomName": "Workshop"},
            "inventory": [[0, {"Name": "minecraft:diamond", "Count": 4}]],
        }
        encoded = encode_schematic(
            {"size": [3, 1, 2]},
            [{"type": "minecraft:chest", "states": {}}],
            records,
            block_entities={(2, 0, 1): entity},
        )
        decoded = decode_schematic(encoded.payload, encoded.sha256_hex)
        self.assertEqual(decoded.header["format_version"], 2)
        self.assertEqual(decoded.block_entities, {(2, 0, 1): entity})

    def test_legacy_v1_payload_remains_readable(self):
        records = bytearray()
        append_record(records, 0, 0, 0, 0)
        header = {
            "format_version": 1,
            "size": [1, 1, 1],
            "block_count": 1,
            "palette_count": 1,
            "palette": [{"type": "minecraft:stone", "states": {}}],
        }
        header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
        payload = zlib.compress(PREFIX.pack(MAGIC, 1, len(header_bytes), 1) + header_bytes + records)
        decoded = decode_schematic(payload)
        self.assertEqual(decoded.block_count, 1)
        self.assertEqual(decoded.block_entities, {})

    def test_checksum_rejected(self):
        encoded = encode_schematic({"size": [1, 1, 1]}, [], b"", 6)
        with self.assertRaises(SchematicCodecError):
            decode_schematic(encoded.payload, "0" * 64)


if __name__ == "__main__":
    unittest.main()
