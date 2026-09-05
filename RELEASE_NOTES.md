# Release Notes: v1.7.0

## BlockData-aware cloud schematics

v1.7.0 integrates the optional [`endstone-blockdata`](https://github.com/TheNINJALLO/endstone-blockdata-api) service so native cloud saves and backups can retain supported block-entity data instead of only base block types and states.

- Save scans use bounded native region captures on Endstone's primary thread.
- Canonical actor NBT and occupied container slots are stored per relative block coordinate.
- Typed NBT byte, short, long, and float values survive the storage round trip.
- Paste writes and verifies the base block first, then applies actor NBT and container inventory patches.
- Empty destination slots are cleared explicitly so a paste cannot leave stale items behind.
- Block-entity coordinates rotate with the placement.
- Undo and redo retain the before/after metadata as well as block types and states.
- `/schem status` reports the connected BlockData API version and adapter.

BlockData remains an optional runtime integration. The schematic plugin does not pin its Python package because its native plugin and CPython bridge must come from the same exact BlockData/BDS/Endstone release bundle.

## NSCM v2 and compatibility

NSCM v2 adds a dedicated compressed sparse block-entity section after the JSON header. Metadata is not deduplicated into the block palette, because two containers with identical block states can contain different names, items, or NBT.

- v1.7.0 reads existing NSCM v1 MySQL rows and native backups.
- New v1.7.0 saves use NSCM v2, even when the optional metadata section is empty.
- The MySQL schema and Bedrock add-on do not change.
- Every server sharing the cloud database should be upgraded before newly saved v2 entries are used.
- Native `.nscm` exports retain the metadata section exactly. Sponge v3 conversion remains block-state-only for Bedrock actor data.

With `blockdata.strict_restore = true`, a destination without the required API or write capability stops on the first retained-data failure and keeps partial undo history. `blockdata.max_uncompressed_mb = 64` bounds in-memory metadata separately from the spill-to-disk base-block record pipeline.

## Large-paste safeguards retained

The v1.6 watchdog and bounded-memory protections remain active:

- Paste, undo, and redo obey both block-count and wall-clock budgets.
- Native chunk holds use deferred release when supported.
- Active operations continue when the initiating player disconnects.
- Base block records, rotated plans, downloads, uploads, and undo journals remain streamed or spillable.
- Packet-safe MySQL chunks and SHA-256 verification are unchanged.

## Validation

The v1.7.0 release passes 72 automated tests. New coverage exercises NSCM v1/v2 compatibility, bounded save capture and rollback, in-memory and streaming metadata round trips, typed NBT reconstruction, inventory clearing, rotation, metadata-aware paste history, and strict failure behavior alongside the existing large-paste, database, chunk, and export suites.

## Runtime compatibility

- Endstone API 0.11
- Python 3.10 or newer for the schematic plugin
- Matching platform-specific BlockData native plugin and CPython bridge when metadata retention is enabled
- Existing MySQL rows remain readable
- No database migration
- No Bedrock add-on change
