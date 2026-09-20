# Changelog

## 1.8.1 - 2026-09-20

- Fixed false paste verification failures when Endstone resolves legacy block names or fills default states omitted by saved palettes.
- Cache the resolved type and complete states per palette entry; use the same resolved data for unchanged checks and retries without rewriting saved palettes.
- Preserve strict detection of ignored writes and wrong orientations, available partial undo, chunk checks and pacing limits.
- Split paste failure details into bounded chat messages while retaining the full server-console error.
- Added regressions and real BDS before/after block-write validation; see `docs/validation-1.8.1.md`.

## 1.8.0 - 2026-09-19

- Added shared categories, save destination selection, moving existing saves, and paginated category/library menus and commands. Existing saves remain intact under Uncategorized; omitted destinations preserve categories on overwrite.
- Added two tables through an additive schema upgrade. Category assignment and payload saves commit together; moves never rewrite block payloads.
- Added a shared 5 ms scan budget, rotating builder scheduling, and small BlockData capture batches.
- Require positive chunk residency, including the loaded flag on enumerated chunks when available. Discard and retry source regions if a chunk loses residency between ticks; stop unverified partial pastes while retaining available undo.
- Prefer native plugin tickets and reference-count native holds across jobs, so one job cannot release another job's chunk. Cache positively held legacy source chunks briefly with fresh completion checks.
- Lower the default adaptive client update ceiling from 1,200 to 256. Migrate that prior shipped value while preserving other custom limits and fixed pacing.
- Added real MariaDB tests for schema upgrades, inline/chunked payload preservation, namespace isolation, pagination, and atomic rollback; added form and chunk/pacing regressions.

## 1.7.3 - 2026-09-13

- Added the native `blockdata_api` plugin as an optional load dependency.
- Retry unavailable BlockData detection on the next tick after startup and every 100 ticks afterward. Defer connection while saves or pastes are active so metadata handling stays consistent within each operation.
- Avoid repeated warnings for an unchanged detection error; log the installed API version and adapter when connection succeeds.
- Identify missing, disabled, or unregistered native providers and include the imported API version in missing-service diagnostics. Identify the active adapter when block-entity NBT support is absent.
- Clarified that Schematic Cloud uses the installed compatible provider without a release-number pin or automatic download; `v2` is the service ABI.
- Passed 112 tests, including delayed provider registration, retry pacing, active-job deferral, provider diagnostics, and release-independent compatibility. Live server detection still requires checking the installed native plugin and startup log.

## 1.7.2 - 2026-09-13

- Fixed paste starvation when chunk residency checks consume the whole 10 ms work budget before placement starts. A confirmed resident chunk now permits one record before yielding.
- Avoided enumerating every loaded chunk twice per paste tick on API 0.11: positively verified, plugin-held paste chunks refresh every 10 ticks and at completion. Native checks, source scans, and unloaded chunks still use immediate checks.
- Skipped stabilization waits for already resident chunks after a native hold is acquired and verified.
- Replaced the fixed 256-change ceiling with adaptive pacing up to 1,200 changes: start at 256, increase on healthy server ticks, and back off on lag. Kept the shared record and time budgets.
- Migrated the v1.7.1 default change limit automatically while preserving other custom limits and fixed limits when adaptive pacing is disabled.
- Added throughput, work timings, chunk wait counts, and current/maximum change budgets to `/schem status`.
- Passed 101 tests. In a controlled simulation with 20 ms chunk enumeration, v1.7.1 processed 0 of 4,096 records in 100 ticks; v1.7.2 completed all 4,096 in 18 ticks with three enumerations. This is a regression simulation, not a live BDS benchmark.

## 1.7.1 - 2026-09-13

- Group streamed paste records into one contiguous range per destination chunk, eliminating repeated load/stabilization cycles across planning batches.
- Reserve spillable output ranges and scatter bounded batches into them, retaining source order within each chunk and the existing temporary-space limits.
- Cache native palette data across paste ticks; skip unchanged blocks before optional BlockData undo capture.
- Add a shared `paste_changed_blocks_per_tick = 256` limit for paste/undo/redo and `chunk_loads_per_tick = 1` for save/paste ticket requests. Existing configs receive both settings automatically.
- Count failed writes against shared limits and rotate scheduling after a slow job to prevent starvation.
- Add regression coverage for delayed new-world chunk generation, complete placement and undo capture, all rotations, failed writes, concurrent jobs, and temporary-file cleanup.

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
