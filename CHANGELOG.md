# Changelog

## 1.7.0 - 2026-09-04

- Integrated the optional `endstone-blockdata` live service for canonical block-entity NBT and container inventory capture and restoration.
- Added NSCM format v2 with a dedicated compressed, sparse block-entity section while retaining full read compatibility with NSCM v1 cloud rows and backups.
- Preserved typed NBT byte, short, long, and float values across JSON storage and restored empty container slots explicitly.
- Rotated block-entity coordinates with their base blocks and included metadata in paste undo/redo history.
- Added strict restoration, metadata size limits, startup diagnostics, status output, and failure accounting.
- Added BlockData, NSCM v1/v2, rotation, streaming, paste, and history regression tests.
- Added a dedicated `commands.md` command and usage reference.

## 1.6.1 - 2026-09-04

- Added a wall-clock paste budget so costly block writes yield before monopolizing the Bedrock server tick.
- Switched native Endstone chunk-hold cleanup to deferred unload requests, avoiding synchronous save/unload stalls across large regions.
- Kept active save, paste, undo, and redo operations running when the initiating player disconnects.
- Added regression coverage for paste yielding, deferred chunk release, and disconnect-safe jobs.
- Kept active save, paste, undo, and redo jobs running when their initiating player disconnects.

## 1.6.0 - 2026-08-06

- Replaced giant in-memory save buffers with spill-to-disk record streams.
- Added incremental zlib compression into temporary payload files.
- Added file-streamed packet-safe MySQL uploads and checksum-verified downloads.
- Added bounded-memory native cloud-to-disk exports.
- Added bounded-output streaming decompression.
- Added batch-bounded, spillable rotated paste planning.
- Moved large undo/redo record journals onto spillable stores.
- Added temporary workspace capacity and free-disk checks.
- Added orphaned streaming-file cleanup after unclean shutdowns.
- Added streaming status details and automatic configuration migration.
- Added record-store, codec, planner, MySQL file-streaming, disk-copy, and multi-million-record memory regression tests.

## 1.5.0 - 2026-08-06

- Added packet-safe transactional MySQL payload chunk storage with retry handling.
- Added missing custom-block policies: skip, air, fallback, or abort.
- Added per-chunk and whole-payload checksum verification.

## 1.4.1 - 2026-07-26

- Replaced blind 24-command startup ticking-area cleanup with a persistent owned-ticket journal.
- Delayed crash-recovery cleanup until dimensions are initialized.
- Reused one legacy ticket slot and name for the lifetime of each save/paste job.
- Added regression coverage for clean reboot silence and journal recovery.

## 1.4.0 - 2026-07-26

- Added guaranteed per-chunk ownership for save, paste, undo, and redo jobs.
- Added legacy Endstone 0.11 ticking-area fallback with preload and deterministic cleanup.
- Added chunk stabilization, timeout, safe save-region retry, and configurable active ticket limits.
- Added save/download/paste integrity validation and per-block write readback.
- Changed paste completion to fail loudly on incomplete accounting or unverifiable writes.
- Existing v1.3.0-or-earlier schematics should be re-saved when unloaded chunks may have been involved.

## 1.3.0 - 2026-07-26

- Added Sponge Schematic v3 export for WorldEdit and Amulet.
- Added cloud-menu and command export actions.
- Added conversion reports and pure-Python NBT validation.
- Added automatic `[worldedit]` configuration migration.
- Fresh native backups now default to `.nscm`; existing configurations remain unchanged.

## 1.2.0 - 2026-07-26

- Added operator-or-`architect` access enforcement across all player-facing paths.
- Added recursive config migration that preserves existing values.
- Added configurable disk schematic directory with absolute-path support.
- Added atomic, checksummed `.schem` exports and metadata sidecars.
- Added cloud form actions for disk backup, backup-and-remove, permanent removal, and archive.
- Added permanent MySQL deletion through `DELETE`.
- Added `/schem export`, `/schem backup-remove`, `/schem remove`, `/schem archive`, and `/schem diskpath`.
- Kept `/schem delete` as an alias of permanent removal.
- Added disk, access, and config-migration tests.

## 1.1.0 - 2026-07-26

- Added selection particles, undo/redo, and placement confirmation.
- Added undo, redo, and confirm tool items.
- Added duplicate interaction suppression.

## 1.0.3 - 2026-07-26

- Added stale-install diagnostics and interaction debounce.

## 1.0.2 - 2026-07-26

- Added legacy and modern dimension compatibility.

## 1.0.1 - 2026-07-26

- Fixed Endstone event annotation registration.

## 1.0.0 - 2026-07-26

- Initial shared MySQL schematic library release.
