"""Endstone form interface for Ninj-OS Schematics."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from endstone.form import ActionForm, ModalForm, TextInput, Toggle

if TYPE_CHECKING:
    from endstone import Player
    from .plugin import NinjOSSchematicsPlugin


class SchematicForms:
    def __init__(self, plugin: "NinjOSSchematicsPlugin") -> None:
        self.plugin = plugin

    def _allowed(self, player: "Player") -> bool:
        return self.plugin.require_schematic_access(player)

    def open_main(self, player: "Player") -> None:
        if not self._allowed(player):
            return
        selection = self.plugin.selections.get(player.unique_id)
        selection_text = "Not selected"
        if selection and selection.complete:
            sx, sy, sz = selection.size
            selection_text = f"{sx} × {sy} × {sz} ({selection.volume:,} blocks)"
        placement = self.plugin.placements.get(player.unique_id)
        placement_text = "None"
        if placement:
            placement_text = f"{placement.name} | {placement.rotation}°"
        form = ActionForm(
            title="Ninj-OS Schematic Cloud",
            content=(
                f"Selection: {selection_text}\n"
                f"Placement: {placement_text}\n\n"
                "Blueprint data is shared through MySQL and can be exported to native disk or Sponge v3 for WorldEdit and Amulet."
            ),
        )
        form.add_button("Save Current Selection", on_click=self.open_save)
        form.add_button("Clear Selection", on_click=self.plugin.clear_selection)
        form.add_button("Browse Cloud Library", on_click=lambda p: self.plugin.request_list(p, ""))
        form.add_button("Search Cloud Library", on_click=self.open_search)
        form.add_button("Current Placement", on_click=self.open_placement)
        form.add_button(
            f"Undo Last Paste ({len(self.plugin.undo_history.get(player.unique_id, []))})",
            on_click=self.plugin.undo,
        )
        form.add_button(
            f"Redo Last Undo ({len(self.plugin.redo_history.get(player.unique_id, []))})",
            on_click=self.plugin.redo,
        )
        form.add_button("Give Schematic Tools", on_click=self.plugin.give_tools)
        form.add_button("Job Status", on_click=self.open_status)
        form.add_button("Test MySQL Connection", on_click=self.plugin.request_db_test)
        form.add_button("Cancel Active Job", on_click=lambda p: self.plugin.cancel_all(p, notify=True))
        player.send_form(form)

    def open_save(self, player: "Player") -> None:
        if not self._allowed(player):
            return
        selection = self.plugin.selections.get(player.unique_id)
        if not selection or not selection.complete:
            player.send_error_message("Select both corners before saving a schematic.")
            return
        sx, sy, sz = selection.size
        schematic_config = self.plugin.config.get("schematics", {})
        default_air = bool(schematic_config.get("include_air_default", True))
        default_overwrite = bool(schematic_config.get("allow_overwrite_default", False))
        controls = [
            TextInput("Cloud name", "castle-gate"),
            TextInput("Description", "Optional blueprint notes", ""),
            Toggle("Include air blocks (clears the destination volume)", default_air),
            Toggle("Overwrite an existing blueprint with this name", default_overwrite),
        ]

        def submitted(submitter: "Player", response: str) -> None:
            try:
                values = json.loads(response)
                name = str(values[0])
                description = str(values[1] or "")
                include_air = bool(values[2])
                overwrite = bool(values[3])
            except (ValueError, TypeError, IndexError, json.JSONDecodeError):
                submitter.send_error_message("The save form returned invalid data.")
                return
            self.plugin.start_save(submitter, name, description, include_air, overwrite)

        player.send_form(
            ModalForm(
                title=f"Save {sx} × {sy} × {sz} Selection",
                controls=controls,
                submit_button="Scan and Upload",
                on_submit=submitted,
            )
        )

    def open_search(self, player: "Player") -> None:
        if not self._allowed(player):
            return
        def submitted(submitter: "Player", response: str) -> None:
            try:
                values = json.loads(response)
                search = str(values[0] or "")
            except (ValueError, TypeError, IndexError, json.JSONDecodeError):
                submitter.send_error_message("The search form returned invalid data.")
                return
            self.plugin.request_list(submitter, search)

        player.send_form(
            ModalForm(
                title="Search Cloud Schematics",
                controls=[TextInput("Name or description", "castle, spawn, dungeon...")],
                submit_button="Search",
                on_submit=submitted,
            )
        )

    def show_library(self, player: "Player", rows: list[dict[str, Any]], search: str = "") -> None:
        if not self._allowed(player):
            return
        if not rows:
            player.send_message("§eNo cloud schematics matched that search.")
            return
        form = ActionForm(
            title="Cloud Schematic Library",
            content=f"{len(rows)} blueprint(s){f' matching {search}' if search else ''}. Select one to preview.",
        )
        for row in rows[:50]:
            name = str(row["name"])
            size = f"{row['size_x']}×{row['size_y']}×{row['size_z']}"
            author = str(row.get("author_name", "Unknown"))
            form.add_button(
                f"{row.get('display_name') or name}\n§7{name} | {size} | {author}",
                on_click=lambda p, selected=dict(row): self.open_library_item(p, selected),
            )
        form.add_button("Search Again", on_click=self.open_search)
        player.send_form(form)

    def open_library_item(self, player: "Player", row: dict[str, Any]) -> None:
        if not self._allowed(player):
            return
        name = str(row["name"])
        content = (
            f"Cloud name: {name}\n"
            f"Size: {row['size_x']} × {row['size_y']} × {row['size_z']}\n"
            f"Stored blocks: {int(row['block_count']):,}\n"
            f"Author: {row.get('author_name', 'Unknown')}\n"
            f"Source server: {row.get('source_server', 'Unknown')}\n"
            f"Description: {row.get('description') or 'None'}"
        )
        form = ActionForm(title=str(row.get("display_name") or name), content=content)
        form.add_button("Load and Preview", on_click=lambda p: self.plugin.request_load(p, name))
        disk_ready = (
            self.plugin.disk_store is not None
            and self.plugin.disk_store.settings.enabled
        )
        if disk_ready:
            form.add_button(
                "Save Native Copy to Disk",
                on_click=lambda p: self.plugin.request_export_to_disk(p, name),
            )
        worldedit_ready = (
            self.plugin.worldedit_store is not None
            and self.plugin.worldedit_store.settings.enabled
        )
        if worldedit_ready:
            form.add_button(
                "Export WorldEdit / Amulet (.schem)",
                on_click=lambda p: self.plugin.request_export_worldedit(p, name),
            )
        if self.plugin._hard_delete_enabled:
            if disk_ready:
                form.add_button(
                    "Save to Disk + Remove from MySQL",
                    on_click=lambda p: self.open_mysql_removal_confirmation(
                        p, name, backup_first=True
                    ),
                )
            form.add_button(
                "Remove from MySQL",
                on_click=lambda p: self.open_mysql_removal_confirmation(
                    p, name, backup_first=False
                ),
            )
        form.add_button(
            "Archive in MySQL",
            on_click=lambda p: self.open_archive_confirmation(p, name),
        )
        form.add_button("Back to Library", on_click=lambda p: self.plugin.request_list(p, ""))
        player.send_form(form)

    def open_mysql_removal_confirmation(
        self, player: "Player", name: str, *, backup_first: bool
    ) -> None:
        if not self._allowed(player):
            return
        if backup_first:
            content = (
                f"Save '{name}' to the configured disk folder and then permanently remove "
                "its row and payload from MySQL? The disk copy is written and verified first."
            )
            action = lambda p: self.plugin.request_export_to_disk(
                p, name, remove_from_mysql=True
            )
            button = "Confirm Backup + Remove"
        else:
            content = (
                f"Permanently remove '{name}' from MySQL? This deletes the database row and "
                "payload for every connected server. This cannot be undone unless a disk backup exists."
            )
            action = lambda p: self.plugin.request_remove_from_mysql(p, name)
            button = "Confirm Permanent Removal"
        form = ActionForm(title="Confirm MySQL Removal", content=content)
        form.add_button(button, on_click=action)
        form.add_button("Cancel", on_click=lambda p: self.plugin.request_list(p, ""))
        player.send_form(form)

    def open_archive_confirmation(self, player: "Player", name: str) -> None:
        if not self._allowed(player):
            return
        form = ActionForm(
            title="Archive Cloud Schematic",
            content=(
                f"Archive '{name}'? It will disappear from the active cloud library but remain "
                "stored in MySQL for database-level recovery."
            ),
        )
        form.add_button("Confirm Archive", on_click=lambda p: self.plugin.request_archive(p, name))
        form.add_button("Cancel", on_click=lambda p: self.plugin.request_list(p, ""))
        player.send_form(form)

    def open_placement(self, player: "Player") -> None:
        if not self._allowed(player):
            return
        placement = self.plugin.placements.get(player.unique_id)
        if not placement:
            player.send_error_message("Load a cloud schematic first.")
            return
        size = self.plugin.placement_size(placement)
        anchor = placement.anchor
        form = ActionForm(
            title=f"Place: {placement.name}",
            content=(
                f"Rotation: {placement.rotation}°\n"
                f"Rotated size: {size[0]} × {size[1]} × {size[2]}\n"
                f"Anchor: {anchor.x}, {anchor.y}, {anchor.z}\n"
                f"Dimension: {placement.dimension_id}"
            ),
        )
        form.add_button(
            "Rotate Clockwise 90°",
            on_click=lambda p: self.plugin.rotate_placement(p, 90, absolute=False),
        )
        form.add_button(
            "Rotate Counterclockwise 90°",
            on_click=lambda p: self.plugin.rotate_placement(p, -90, absolute=False),
        )
        form.add_button("Move Anchor to My Feet", on_click=self.plugin.anchor_at_player)
        form.add_button("Refresh Bounding Box", on_click=self.plugin.refresh_preview)
        form.add_button("Review and Confirm Placement", on_click=self.open_paste_confirmation)
        form.add_button("Cancel Placement", on_click=self.plugin.cancel_placement)
        player.send_form(form)

    def open_paste_confirmation(self, player: "Player") -> None:
        if not self._allowed(player):
            return
        placement = self.plugin.placements.get(player.unique_id)
        if not placement:
            player.send_error_message("There is no active placement.")
            return
        form = ActionForm(
            title="Confirm Schematic Paste",
            content=(
                f"Paste '{placement.name}' at {placement.anchor.x}, {placement.anchor.y}, {placement.anchor.z}?\n\n"
                "The operation is divided across server ticks. Existing blocks in the target volume may be replaced."
            ),
        )
        form.add_button("Confirm Placement and Paste", on_click=self.plugin.start_paste)
        form.add_button("Return to Placement", on_click=self.open_placement)
        player.send_form(form)

    def open_status(self, player: "Player") -> None:
        if not self._allowed(player):
            return
        player.send_message(self.plugin.status_text(player))
