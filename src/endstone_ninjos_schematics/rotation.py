"""Coordinate and Bedrock block-state rotation helpers."""

from __future__ import annotations

from typing import Any

VALID_ROTATIONS = (0, 90, 180, 270)
CARDINALS = ("north", "east", "south", "west")
FACING_DIRECTION = {2: 0, 5: 1, 3: 2, 4: 3}
FACING_DIRECTION_REVERSE = {value: key for key, value in FACING_DIRECTION.items()}

# Bedrock rail_direction values. Curves and ascents are rotated independently.
RAIL_GROUPS = (
    (0, 1),          # north-south / east-west
    (2, 5, 3, 4),    # ascending east, south, west, north
    (6, 7, 8, 9),    # south-east, south-west, north-west, north-east
)


def normalize_rotation(rotation: int) -> int:
    result = int(rotation) % 360
    if result not in VALID_ROTATIONS:
        raise ValueError("rotation must be 0, 90, 180, or 270 degrees")
    return result


def rotated_size(size: tuple[int, int, int], rotation: int) -> tuple[int, int, int]:
    rotation = normalize_rotation(rotation)
    sx, sy, sz = size
    return (sz, sy, sx) if rotation in (90, 270) else (sx, sy, sz)


def rotate_coord(
    dx: int,
    dy: int,
    dz: int,
    size: tuple[int, int, int],
    rotation: int,
) -> tuple[int, int, int]:
    rotation = normalize_rotation(rotation)
    sx, _, sz = size
    if rotation == 0:
        return dx, dy, dz
    if rotation == 90:
        return sz - 1 - dz, dy, dx
    if rotation == 180:
        return sx - 1 - dx, dy, sz - 1 - dz
    return dz, dy, sx - 1 - dx


def _rotate_cardinal(value: str, quarter_turns: int) -> str:
    lowered = value.lower()
    if lowered not in CARDINALS:
        return value
    return CARDINALS[(CARDINALS.index(lowered) + quarter_turns) % 4]


def _rotate_rail(value: int, quarter_turns: int) -> int:
    if value in RAIL_GROUPS[0]:
        return value if quarter_turns % 2 == 0 else (1 if value == 0 else 0)
    for group in RAIL_GROUPS[1:]:
        if value in group:
            return group[(group.index(value) + quarter_turns) % 4]
    return value


def rotate_states(states: dict[str, Any], rotation: int) -> dict[str, Any]:
    """Rotate common Bedrock orientation states while preserving unknown states."""
    rotation = normalize_rotation(rotation)
    quarter_turns = rotation // 90
    if quarter_turns == 0:
        return dict(states)
    result = dict(states)
    for key, value in list(result.items()):
        lowered_key = key.lower()
        if isinstance(value, str) and lowered_key in {
            "cardinal_direction",
            "minecraft:cardinal_direction",
            "facing",
            "direction",
        }:
            result[key] = _rotate_cardinal(value, quarter_turns)
        elif isinstance(value, int) and lowered_key in {"facing_direction", "minecraft:facing_direction"}:
            index = FACING_DIRECTION.get(value)
            if index is not None:
                result[key] = FACING_DIRECTION_REVERSE[(index + quarter_turns) % 4]
        elif isinstance(value, int) and lowered_key in {
            "ground_sign_direction",
            "minecraft:ground_sign_direction",
            "rotation",
        }:
            result[key] = (value + quarter_turns * 4) % 16
        elif isinstance(value, str) and lowered_key in {"pillar_axis", "minecraft:pillar_axis", "axis"}:
            if quarter_turns % 2 == 1 and value.lower() in {"x", "z"}:
                result[key] = "z" if value.lower() == "x" else "x"
        elif isinstance(value, int) and lowered_key in {"rail_direction", "minecraft:rail_direction"}:
            result[key] = _rotate_rail(value, quarter_turns)
        elif isinstance(value, int) and lowered_key in {"weirdo_direction", "minecraft:weirdo_direction"}:
            # Common Bedrock stairs mapping: east=0, west=1, south=2, north=3.
            order = (3, 0, 2, 1)  # north, east, south, west -> stored value
            if value in order:
                cardinal_index = order.index(value)
                result[key] = order[(cardinal_index + quarter_turns) % 4]
    return result
