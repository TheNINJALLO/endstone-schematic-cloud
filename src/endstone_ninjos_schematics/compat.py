"""Compatibility helpers for Endstone dimension API variants.

Endstone releases before the identifier migration expose ``Dimension.name``.
Newer 0.11 builds expose ``Dimension.id``. The plugin accepts either shape so
it can run across patch-level API transitions without hard-crashing tools.
"""

from __future__ import annotations

from typing import Any


_DIMENSION_ALIASES: dict[str, tuple[str, ...]] = {
    "minecraft:overworld": ("minecraft:overworld", "overworld", "Overworld"),
    "minecraft:nether": ("minecraft:nether", "nether", "Nether"),
    "minecraft:the_end": (
        "minecraft:the_end",
        "the_end",
        "the end",
        "The End",
        "end",
        "End",
    ),
}


def _normalise_dimension_text(value: Any) -> str:
    text = str(value).strip()
    lowered = text.lower().replace("-", "_").replace(" ", "_")
    if lowered.startswith("dimension.type."):
        lowered = lowered.rsplit(".", 1)[-1]
    if lowered.startswith("type."):
        lowered = lowered.rsplit(".", 1)[-1]
    return lowered


def _canonical_dimension_id(value: Any) -> str | None:
    normalised = _normalise_dimension_text(value)
    for canonical, aliases in _DIMENSION_ALIASES.items():
        if normalised == _normalise_dimension_text(canonical):
            return canonical
        if any(normalised == _normalise_dimension_text(alias) for alias in aliases):
            return canonical
    if normalised in {"0", "overworld"}:
        return "minecraft:overworld"
    if normalised in {"1", "nether"}:
        return "minecraft:nether"
    if normalised in {"2", "the_end", "end"}:
        return "minecraft:the_end"
    return None


def dimension_identifier(dimension: Any) -> str:
    """Return the dimension key exposed by the running Endstone build.

    ``name`` is preferred when present because older 0.11 builds use it for
    ``Level.get_dimension`` lookups. Newer builds fall back to ``id``.
    A type-based canonical fallback prevents a vague ``AttributeError`` if a
    downstream build exposes neither property.
    """

    for attribute in ("name", "id"):
        try:
            value = getattr(dimension, attribute)
        except (AttributeError, RuntimeError):
            continue
        if value is not None and str(value).strip():
            return str(value)

    try:
        dimension_type = getattr(dimension, "type")
    except (AttributeError, RuntimeError):
        dimension_type = None
    if dimension_type is not None:
        canonical = _canonical_dimension_id(dimension_type)
        if canonical:
            return canonical

        for attribute, canonical in (
            ("OVERWORLD", "minecraft:overworld"),
            ("NETHER", "minecraft:nether"),
            ("THE_END", "minecraft:the_end"),
        ):
            marker = getattr(dimension, attribute, getattr(type(dimension), attribute, None))
            if marker is not None and dimension_type == marker:
                return canonical

    raise AttributeError(
        f"Unable to identify Endstone dimension object {type(dimension).__name__}; "
        "expected an 'id', 'name', or recognised 'type' property."
    )


def _lookup_candidates(identifier: Any) -> list[str]:
    original = str(identifier).strip()
    candidates = [original]
    canonical = _canonical_dimension_id(original)
    if canonical:
        for candidate in (canonical, *_DIMENSION_ALIASES[canonical]):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def resolve_dimension(level: Any, identifier: Any) -> Any | None:
    """Resolve a stored dimension key against old and new Endstone APIs."""

    getter = getattr(level, "get_dimension", None)
    if callable(getter):
        for candidate in _lookup_candidates(identifier):
            try:
                dimension = getter(candidate)
            except (TypeError, ValueError, RuntimeError):
                continue
            if dimension is not None:
                return dimension

    try:
        dimensions = list(getattr(level, "dimensions"))
    except (AttributeError, TypeError, RuntimeError):
        dimensions = []

    expected = {_normalise_dimension_text(value) for value in _lookup_candidates(identifier)}
    for dimension in dimensions:
        values: list[Any] = []
        for attribute in ("id", "name", "type"):
            try:
                value = getattr(dimension, attribute)
            except (AttributeError, RuntimeError):
                continue
            if value is not None:
                values.append(value)
        for value in values:
            normalised = _normalise_dimension_text(value)
            canonical = _canonical_dimension_id(value)
            if normalised in expected or (
                canonical is not None and _normalise_dimension_text(canonical) in expected
            ):
                return dimension
    return None
