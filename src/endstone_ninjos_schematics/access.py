"""Central role gate for schematic access."""

from __future__ import annotations

from typing import Any


def player_has_schematic_access(player: Any, architect_tag: str = "architect") -> bool:
    """Return True only for operators or players carrying the architect tag."""

    try:
        if bool(getattr(player, "is_op")):
            return True
    except (AttributeError, RuntimeError, TypeError):
        pass

    wanted = (architect_tag or "architect").strip().casefold()
    try:
        tags = getattr(player, "scoreboard_tags")
    except (AttributeError, RuntimeError):
        tags = ()
    try:
        return any(str(tag).strip().casefold() == wanted for tag in tags or ())
    except (TypeError, RuntimeError):
        return False
