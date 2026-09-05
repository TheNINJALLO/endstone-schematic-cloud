# Ninj-OS Schematic Cloud

Ninj-OS Schematic Cloud is an **Endstone API 0.11** Python plugin for saving, sharing, previewing, rotating, confirming, pasting, undoing, archiving, and exporting Minecraft Bedrock structures.

Current release: **v1.6.1**, with watchdog-safe large-paste scheduling and deferred chunk release.

The primary library remains the remote MySQL database so every connected server sees new blueprints immediately. The plugin can also write two disk formats:

- **Native NSCM**: exact compressed cloud payload for lossless Ninj-OS backup and restore.
- **Sponge Schematic v3 (`.schem`)**: portable export for modern WorldEdit and Amulet.

## Bounded-memory saves and pastes

Version 1.6.0 changes the large-schematic pipeline from whole-payload memory copies to spillable streams. Small operations remain in RAM; large record buffers cross an 8 MiB threshold and continue in temporary files. Compression, MySQL transfer, decompression, rotated planning, undo capture, and native disk export then consume those files in bounded pieces.

```toml
[streaming]
enabled = true
memory_spill_threshold_mb = 8
plan_batch_records = 32768
temp_directory = "streaming_work"
max_temp_workspace_mb = 16384
minimum_free_disk_mb = 1024
cleanup_orphans_on_startup = true
```

A 16,000,000-record full-volume schematic contains about 244 MiB of raw fixed-width records. Earlier builds could hold several complete copies at once. v1.6.0 keeps the bulk data on disk and checks workspace capacity before starting. Undo is automatically omitted for an individual paste above `history.max_blocks_per_operation`, preventing two additional full record streams unless the administrator explicitly raises that ceiling.

## Packet-safe MySQL payload storage

Version 1.6.0 keeps schematic metadata in the original `ninjos_schematics` table and stores large compressed payloads in packet-safe rows in `ninjos_schematic_payload_chunks`. Existing inline `LONGBLOB` records remain readable.

```toml
[database]
connect_timeout_seconds = 10
read_timeout_seconds = 120
write_timeout_seconds = 120
payload_chunk_size_mb = 2
inline_payload_max_mb = 2
retry_attempts = 3
retry_backoff_seconds = 2.0
```

Uploads are transactional. The plugin verifies the chunk count and byte count before commit. Downloads verify each chunk hash and the complete schematic hash while streaming to disk. This avoids sending a multi-million-block schematic as one oversized MySQL packet.

## Missing custom block policy

A schematic may reference a custom block whose behavior pack is missing or whose identifier changed on the destination server. v1.6.0 caches unavailable palette entries and applies a configurable policy instead of repeatedly throwing the same registry exception.

```toml
[placement]
missing_block_policy = "skip"
missing_block_fallback = "minecraft:stone"
missing_block_report_limit = 20
```

Policies are `skip`, `air`, `fallback`, and `abort`. Exact restoration still requires the same behavior pack and block identifiers on the destination server.

## Verified chunk residency for very large builds

Version 1.4.0 and newer hold every source and destination chunk before it reads or writes blocks. Newer Endstone builds use native chunk tickets. Older 0.11 runtimes, including builds that expose `Dimension.name` and `Dimension.loaded_chunks` but not `load_chunk`, use a temporary preloaded Bedrock ticking area for one chunk at a time.

The plugin no longer trusts a chunk merely because a player happens to be near it. It acquires its own ticket, waits for the chunk to report loaded, waits a stabilization delay, performs the batch, verifies residency again, and only then releases the ticket. Save regions are rolled back and rescanned if residency drops. Paste writes are read back and verified.

```toml
[performance]
auto_load_missing_chunks = true
chunk_load_timeout_ticks = 1200
chunk_stabilize_ticks = 4
max_chunk_retries = 3
legacy_tickingarea_fallback = true
legacy_tickingarea_preload = true
legacy_tickingarea_prefix = "njs_schem"
legacy_tickingarea_max_active = 8
verify_paste_writes = true
max_paste_failures = 0
```

Bedrock permits up to 10 ticking areas per world, so the default reserves at most eight temporary plugin slots and leaves room for administrator-created areas. Temporary names are journaled and cleaned on job completion or targeted crash recovery after an unclean shutdown. Cheats must be enabled for the legacy fallback because `/tickingarea` is a Game Directors command.

A v1.3.0-or-earlier cloud entry that was captured while chunks were unloaded cannot be repaired from its checksum. Re-save the original world region with v1.4.0 or newer.

## Access policy

Every player-facing path uses one centralized role check. A player must be either a server operator or carry the scoreboard tag `architect`.

```text
/tag <player> add architect
/tag <player> remove architect
```

Unauthorized players are rejected before forms, tools, previews, world edits, disk access, export conversion, or database operations run.

## Cloud library actions

Select a cloud schematic to access:

- **Load and Preview**
- **Save Native Copy to Disk**
- **Export WorldEdit / Amulet (`.schem`)**
- **Save to Disk + Remove from MySQL**
- **Remove from MySQL**
- **Archive in MySQL**

Native exports preserve the exact NSCM payload. WorldEdit/Amulet exports decode the cloud payload off-thread and produce gzip-compressed big-endian NBT following Sponge Schematic v3.

## WorldEdit and Amulet export

Default configuration:

```toml
[worldedit]
enabled = true
directory = "worldedit_schematics"
java_data_version = 4671
overwrite_exports = true
write_conversion_report = true
max_file_size_mb = 1024
```

A relative directory is created below the plugin data folder. An absolute directory may point directly at a shared or mounted WorldEdit schematic folder.

Each export can create:

```text
castle.schem
castle.schem.conversion.json
```

The conversion report records remapped identifiers, stripped Bedrock-only state names, and warnings. It is especially useful because Bedrock and Java do not use identical block-state schemas.

### Fidelity boundary

The `.schem` exporter preserves the structure dimensions, block palette, mapped block states, author metadata, source server, and origin offset. Endstone API 0.11 does not expose a stable generic serializer for block-entity NBT, so these are not exported yet:

- Container inventories
- Sign text
- Command-block commands
- Lectern books
- Spawner data
- Entities and biomes

Unknown Bedrock-only vanilla state names are stripped rather than emitting invalid Java properties. The report identifies them. Custom namespaces retain syntactically safe properties to give matching Java mods or data packs a chance to resolve them.

A native schematic saved without air is sparse. Sponge schematics require a complete volume, so missing positions are written as air and a warning is placed in the conversion report.

## Native disk backups

Fresh installations default to `.nscm` so native files are not confused with WorldEdit `.schem` files:

```toml
[disk]
enabled = true
directory = "schematics"
extension = ".nscm"
auto_create_directory = true
write_metadata_sidecar = true
overwrite_cloud_exports = true
max_file_size_mb = 512
```

Existing configurations that already use `.schem` are preserved by automatic config migration. Those older native files are still NSCM payloads and are **not** WorldEdit files.

## Commands

```text
/schem menu
/schem pos1 [x y z]
/schem pos2 [x y z]
/schem clearselection
/schem save <name> [include_air] [overwrite]
/schem list [search]
/schem load <name>
/schem export <name> [overwrite]
/schem export-worldedit <name> [overwrite]
/schem worldeditpath
/schem backup-remove <name> [overwrite]
/schem remove <name>
/schem archive <name>
/schem diskpath
/schem anchor [x y z]
/schem rotate [0|90|180|270|cw|ccw]
/schem preview
/schem paste
/schem confirm
/schem undo
/schem redo
/schem tools
/schem status
/schem cancel
/schem dbtest
/schem version
```

Aliases for WorldEdit export include `/schem worldedit`, `/schem amulet`, and `/schem sponge-v3`.

## Performance model

World reads and writes stay on the Endstone server thread and are divided across ticks. Compression, decompression, MySQL work, native disk I/O, Sponge conversion, gzip/NBT writing, and paste planning run in the bounded worker pool.

Default limits:

- Save scan: 2,500 blocks per tick
- Paste, undo, and redo: up to 1,200 blocks or 10 ms per tick, whichever comes first
- Maximum selection: 2,000,000 blocks
- Worker threads: 2
- Physics during bulk placement: disabled
- Unchanged destination blocks: skipped

## Automatic config migration

At startup, v1.6.1 recursively adds missing settings from the packaged defaults while preserving existing MySQL credentials, paths, limits, and customized values. Exact legacy defaults for the MySQL read/write timeout are migrated from 30 to 120 seconds; administrator-selected values are preserved.

Large pastes also release native Endstone chunk holds through the deferred unload API when it is available. Active scans, pastes, undo, and redo operations continue if their initiating player disconnects; reconnect and use `/schem status` or `/schem cancel` as needed.

## Compatibility

- Endstone API: `0.11`
- Python: 3.10+
- Designed for Bedrock/Endstone 26.x, including 26.30-era servers
- MySQL 8.0+ or MariaDB 10.5+
- Export format: Sponge Schematic v3

See [INSTALL.md](INSTALL.md) for clean upgrade instructions.
