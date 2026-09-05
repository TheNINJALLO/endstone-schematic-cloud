"""Tool interaction listener with cross-instance interaction debouncing."""

from time import monotonic
from typing import Any

from endstone.event import PlayerInteractEvent, PlayerQuitEvent, event_handler


# Endstone/Bedrock can emit more than one PlayerInteractEvent for a single client
# gesture. This guard is module-global on purpose: if duplicate plugin instances
# are accidentally loaded, they still share one debounce ledger.
_RECENT_INTERACTIONS: dict[tuple[str, str, str], float] = {}
_MAX_LEDGER_ENTRIES = 4096


def _player_token(player: Any) -> str:
    for attribute in ("unique_id", "xuid", "name"):
        try:
            value = getattr(player, attribute)
        except (AttributeError, RuntimeError):
            continue
        if value is not None and str(value):
            return str(value)
    return f"object:{id(player)}"


def claim_interaction(
    player: Any,
    item_id: str,
    operation: str,
    window_seconds: float,
    *,
    now: float | None = None,
) -> bool:
    """Return True once per interaction window for a player/tool operation."""

    timestamp = monotonic() if now is None else float(now)
    key = (_player_token(player), item_id, operation)
    previous = _RECENT_INTERACTIONS.get(key)
    if previous is not None and timestamp - previous < max(0.01, window_seconds):
        return False
    _RECENT_INTERACTIONS[key] = timestamp

    if len(_RECENT_INTERACTIONS) > _MAX_LEDGER_ENTRIES:
        cutoff = timestamp - max(5.0, window_seconds * 4.0)
        stale = [entry for entry, seen in _RECENT_INTERACTIONS.items() if seen < cutoff]
        for entry in stale:
            _RECENT_INTERACTIONS.pop(entry, None)
    return True


def clear_player_interactions(player: Any) -> None:
    token = _player_token(player)
    for key in [entry for entry in _RECENT_INTERACTIONS if entry[0] == token]:
        _RECENT_INTERACTIONS.pop(key, None)


class SchematicToolListener:
    def __init__(self, plugin):
        self.plugin = plugin

    @event_handler
    def on_interact(self, event: PlayerInteractEvent) -> None:
        if not event.has_item or event.item is None:
            return

        item_id = self.plugin.item_identifier(event.item)
        tools = self.plugin.tool_ids
        if item_id not in tools.values():
            return

        operation: str | None = None
        if item_id == tools["selector"]:
            if event.has_block and event.action == PlayerInteractEvent.LEFT_CLICK_BLOCK:
                operation = "selector_pos1"
            elif event.has_block and event.action == PlayerInteractEvent.RIGHT_CLICK_BLOCK:
                operation = "selector_pos2"
        elif item_id == tools["placer"]:
            if event.has_block and event.action == PlayerInteractEvent.RIGHT_CLICK_BLOCK:
                operation = "placer_anchor"
        elif item_id == tools["rotator"]:
            if event.action in {
                PlayerInteractEvent.RIGHT_CLICK_BLOCK,
                PlayerInteractEvent.RIGHT_CLICK_AIR,
            }:
                # Treat block and air variants as the same physical click.
                operation = "rotate"
        elif item_id == tools["tablet"]:
            if event.action in {
                PlayerInteractEvent.RIGHT_CLICK_BLOCK,
                PlayerInteractEvent.RIGHT_CLICK_AIR,
            }:
                # Treat block and air variants as the same physical click.
                operation = "menu"
        elif item_id == tools["undo"]:
            if event.action in {
                PlayerInteractEvent.RIGHT_CLICK_BLOCK,
                PlayerInteractEvent.RIGHT_CLICK_AIR,
            }:
                operation = "undo"
        elif item_id == tools["redo"]:
            if event.action in {
                PlayerInteractEvent.RIGHT_CLICK_BLOCK,
                PlayerInteractEvent.RIGHT_CLICK_AIR,
            }:
                operation = "redo"
        elif item_id == tools["confirm"]:
            if event.action in {
                PlayerInteractEvent.RIGHT_CLICK_BLOCK,
                PlayerInteractEvent.RIGHT_CLICK_AIR,
            }:
                operation = "confirm"

        if operation is None:
            return

        # Cancel every duplicate event too, so the tool never performs a vanilla
        # action while only the first event executes plugin behavior.
        event.is_cancelled = True
        player = event.player
        if not self.plugin.require_schematic_access(player):
            return

        window = float(getattr(self.plugin, "_tool_debounce_seconds", 0.45))
        if not claim_interaction(player, item_id, operation, window):
            return

        if operation == "selector_pos1":
            self.plugin.set_selection_from_block(player, 1, event.block)
        elif operation == "selector_pos2":
            self.plugin.set_selection_from_block(player, 2, event.block)
        elif operation == "placer_anchor":
            self.plugin.anchor_from_clicked_block(player, event.block, event.block_face)
        elif operation == "rotate":
            self.plugin.rotate_placement(player, 90, absolute=False)
        elif operation == "menu":
            self.plugin.forms.open_main(player)
        elif operation == "undo":
            self.plugin.undo(player)
        elif operation == "redo":
            self.plugin.redo(player)
        elif operation == "confirm":
            self.plugin.forms.open_paste_confirmation(player)

    @event_handler
    def on_quit(self, event: PlayerQuitEvent) -> None:
        clear_player_interactions(event.player)
        self.plugin.handle_player_quit(event.player)
