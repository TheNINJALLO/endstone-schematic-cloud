# Ninj-OS Schematic Cloud Command Reference

Every command begins with `/schem`. Run `/schem menu` for the form-driven interface or use the commands below directly.

## Access and argument rules

- In-game commands require server operator status or the configured `access.architect_tag` scoreboard tag.
- The console may use only `/schem version` and `/schem dbtest`.
- Coordinates are three absolute block integers: `<x> <y> <z>`.
- Boolean arguments accept `true`, `false`, `yes`, `no`, `on`, `off`, `1`, or `0`.
- Schematic names normalize to lowercase, allow letters, numbers, dots, underscores, and dashes, and are limited to 64 characters.
- Arguments in angle brackets are required. Arguments in square brackets are optional.

## Menu and selection

### `/schem menu`

Opens the main schematic form. The form links selection, cloud storage, placement, history, tools, diagnostics, and cancellation.

Aliases: `/schem ui`, `/schem form`

### `/schem pos1 [x y z]`

Sets the first selection corner. Without coordinates, it uses the player's current block position.

```text
/schem pos1
/schem pos1 -32 64 48
```

Alias: `/schem p1`

### `/schem pos2 [x y z]`

Sets the second selection corner in the same dimension as position one. Selecting in another dimension resets the previous corners.

```text
/schem pos2
/schem pos2 15 92 80
```

Alias: `/schem p2`

### `/schem selection`

Shows the current selection and the same operational information as `/schem status`.

Alias: `/schem sel`

### `/schem clearselection`

Clears both selection corners and their particle outline.

Alias: `/schem clearsel`

## Cloud library

### `/schem save <name> [include_air] [overwrite]`

Scans the selected cuboid across ticks, verifies every source chunk, compresses it, and uploads it to MySQL/MariaDB.

```text
/schem save castle-gate
/schem save castle-gate true false
/schem save castle-gate false true
```

- `include_air=true` saves the complete volume; saved air clears destination blocks.
- `include_air=false` stores only non-air blocks; unspecified destination blocks remain untouched.
- `overwrite=true` replaces an existing entry with the same normalized name.
- When BlockData retention is ready, supported block-entity NBT and container inventories are included automatically.

### `/schem list [search]`

Lists up to 50 active cloud entries, optionally filtered by a search phrase.

```text
/schem list
/schem list castle
```

Alias: `/schem browse`

### `/schem load <name>`

Downloads and checksum-validates a cloud entry, prepares its preview, and sets the initial anchor to the player's position.

```text
/schem load castle-gate
```

### `/schem archive <name>`

Hides a schematic from normal cloud listings while retaining its database row and payload.

```text
/schem archive old-spawn
```

### `/schem remove <name>`

Permanently deletes the cloud row and payload after confirmation. This affects every server using the same database namespace.

```text
/schem remove old-spawn
```

Alias: `/schem delete`

## Placement and preview

### `/schem anchor [x y z]`

Moves the loaded schematic's placement anchor. Without coordinates, it uses the player's current block position.

```text
/schem anchor
/schem anchor 100 64 -250
```

### `/schem rotate [0|90|180|270|cw|ccw]`

Rotates the loaded placement around its anchor. With no argument, `cw`, `right`, or `+`, it rotates 90 degrees clockwise. `ccw`, `left`, or `-` rotates 90 degrees counterclockwise. Numeric arguments set an absolute rotation.

```text
/schem rotate cw
/schem rotate 180
```

Block states and retained BlockData coordinates rotate with the placement.

### `/schem preview`

Refreshes the loaded schematic's particle bounding box without changing the world.

### `/schem paste`

Opens the final placement confirmation screen. It does not start writing blocks by itself.

Alias: `/schem place`

### `/schem confirm`

Builds the bounded-memory, chunk-aware plan and starts the tick-budgeted paste after confirmation.

Alias: `/schem commit`

### `/schem cancel`

Cancels the player's active save scan, paste preparation, paste, or placement preview. If a partially completed paste has history data, the plugin retains a partial undo.

## Undo and redo

### `/schem undo`

Restores the blocks changed by the most recent completed or recorded partial paste. When BlockData is active, captured actor/container metadata is restored too.

### `/schem redo`

Reapplies the most recently undone paste, including retained metadata when available.

Undo and redo are in-memory per-player histories and are cleared on server restart. Their operation and block limits are controlled by `[history]` in `config.toml`.

## Backups and interchange

### `/schem export <name> [overwrite]`

Writes an exact native `.nscm` backup to the configured disk directory. NSCM v2 backups preserve the BlockData sidecar.

```text
/schem export castle-gate
/schem export castle-gate true
```

Aliases: `/schem download`, `/schem disk-save`

### `/schem export-worldedit <name> [overwrite]`

Converts a cloud schematic to Sponge Schematic v3 for modern WorldEdit and Amulet. Bedrock block-entity metadata is not currently translated into the Sponge export.

```text
/schem export-worldedit castle-gate
```

Aliases: `/schem worldedit`, `/schem amulet`, `/schem sponge-v3`

### `/schem backup-remove <name> [overwrite]`

Writes and verifies a native `.nscm` backup, then permanently removes the cloud entry. The database row is deleted only after backup verification succeeds.

Alias: `/schem export-remove`

### `/schem diskpath`

Shows the configured native backup directory.

### `/schem worldeditpath`

Shows the configured Sponge `.schem` export directory.

## Tools and diagnostics

### `/schem tools`

Gives the selection wand, placement anchor, rotator, cloud tablet, undo, redo, and confirm items. The matching behavior and resource packs from `addon/` must be active.

### `/schem status`

Shows database and disk readiness, BlockData retention status, streaming workspace details, selection, placement, active-job progress, and undo/redo counts.

### `/schem dbtest`

Tests the configured MySQL/MariaDB connection and reports the namespace and database server version. Available from the console.

### `/schem version`

Shows the loaded plugin version, build ID, access gate, and Python module path. Use this after an upgrade to identify stale wheel installs. Available from the console.

Alias: `/schem ver`

### `/schem help`

Shows concise command help in chat.

Alias: `/schem ?`

## Recommended workflows

Save and share a build:

```text
/schem pos1
/schem pos2
/schem status
/schem save castle-gate true false
```

Load and paste safely:

```text
/schem load castle-gate
/schem anchor
/schem rotate cw
/schem preview
/schem paste
/schem confirm
/schem status
```

Create a recoverable local backup before cloud removal:

```text
/schem backup-remove castle-gate false
```
