"""Compatibility helpers for keeping Bedrock chunks resident during large jobs.

Newer Endstone builds expose ``is_chunk_loaded``/``load_chunk``/``unload_chunk``.
Older 0.11 runtimes expose only ``Dimension.loaded_chunks``.  The plugin uses
these pure helpers to detect the running API and falls back to temporary named
Bedrock ticking areas when direct plugin tickets are unavailable.
"""

from __future__ import annotations

import hashlib
from typing import Any


def command_dimension_name(identifier: Any) -> str:
    """Translate an Endstone dimension identifier/name into Bedrock command syntax."""

    text = str(identifier).strip().lower().replace("-", "_").replace(" ", "_")
    if text.startswith("minecraft:"):
        text = text.split(":", 1)[1]
    if text.startswith("dimension.type."):
        text = text.rsplit(".", 1)[-1]
    if text.startswith("type."):
        text = text.rsplit(".", 1)[-1]
    aliases = {
        "0": "overworld",
        "overworld": "overworld",
        "1": "nether",
        "nether": "nether",
        "2": "the_end",
        "end": "the_end",
        "theend": "the_end",
        "the_end": "the_end",
    }
    result = aliases.get(text)
    if result is None:
        raise ValueError(f"Unsupported dimension for chunk loading: {identifier!r}")
    return result


def chunk_block_bounds(chunk_x: int, chunk_z: int) -> tuple[int, int, int, int]:
    """Return inclusive X/Z block bounds for a chunk, including negatives."""

    min_x = int(chunk_x) * 16
    min_z = int(chunk_z) * 16
    return min_x, min_z, min_x + 15, min_z + 15


def chunk_loaded_state(dimension: Any, chunk_x: int, chunk_z: int) -> bool | None:
    """Return loaded state across old/new Endstone APIs, or ``None`` if unknowable."""

    checker = getattr(dimension, "is_chunk_loaded", None)
    if callable(checker):
        try:
            return bool(checker(int(chunk_x), int(chunk_z)))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass

    try:
        loaded_chunks = getattr(dimension, "loaded_chunks")
    except (AttributeError, RuntimeError):
        loaded_chunks = None
    if loaded_chunks is not None:
        try:
            for chunk in list(loaded_chunks):
                try:
                    x = int(getattr(chunk, "x"))
                    z = int(getattr(chunk, "z"))
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    continue
                if x == int(chunk_x) and z == int(chunk_z):
                    return True
            return False
        except (RuntimeError, TypeError):
            pass
    return None


def ticket_name(prefix: str, slot: int) -> str:
    """Return a short command-safe deterministic ticking-area name."""

    clean = "".join(character for character in str(prefix).lower() if character.isalnum() or character == "_")
    clean = clean.strip("_") or "njs_schem"
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:4]
    return f"{clean[:20]}_{digest}_{int(slot)}"


def tickingarea_add_command(
    dimension_identifier: Any,
    chunk_x: int,
    chunk_z: int,
    name: str,
    *,
    preload: bool = True,
) -> str:
    """Build a dimension-scoped one-chunk ticking-area add command."""

    dimension = command_dimension_name(dimension_identifier)
    min_x, min_z, max_x, max_z = chunk_block_bounds(chunk_x, chunk_z)
    preload_text = " true" if preload else ""
    return (
        f"execute in {dimension} run tickingarea add "
        f"{min_x} 0 {min_z} {max_x} 0 {max_z} {name}{preload_text}"
    )


def tickingarea_remove_command(dimension_identifier: Any, name: str) -> str:
    """Build a dimension-scoped ticking-area removal command."""

    dimension = command_dimension_name(dimension_identifier)
    return f"execute in {dimension} run tickingarea remove {name}"
