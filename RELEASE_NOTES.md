# Release Notes: v1.6.1

## Watchdog-safe large pastes

v1.6.1 fixes large cloud pastes that could eventually stall the Bedrock main thread, disconnect every player, and stop when the initiating player's disconnect event cancelled the job.

- Paste, undo, and redo now obey a configurable wall-clock budget in addition to the existing block-count limit. The default is 10 ms per server tick.
- Native Endstone chunk holds use deferred `unload_chunk_request()` cleanup when available, avoiding synchronous save/unload work at every chunk boundary.
- Active save, paste, undo, and redo jobs continue when their initiating player disconnects. The player can reconnect and use `/schem status` or `/schem cancel`.
- Older Endstone builds remain supported through the existing synchronous unload and temporary ticking-area fallbacks.

The MySQL schema, native NSCM format, cloud rows, and Bedrock add-on remain compatible. Existing configuration files automatically receive `performance.paste_time_budget_ms = 10` without replacing administrator settings.

## Bounded-memory large schematic pipeline

Large saves and pastes could restart the Bedrock process without a Python traceback because several complete copies of the schematic coexisted in RAM. A full-volume record uses 16 bytes per selected block before Python and container overhead. A 16,065,750-block save therefore accumulated about 245 MiB in its first record buffer alone, then created additional immutable, uncompressed, compressed, rotated-plan, and undo copies.

The v1.6.x pipeline replaces those whole-payload copies with spillable record streams:

- Save records remain in memory only until the configurable spill threshold, then continue in a temporary file.
- Zlib compression reads the records incrementally and writes a compressed payload file.
- MySQL upload reads that payload in packet-safe chunks without loading it as one Python `bytes` object.
- MySQL downloads stream to disk and are hash-verified while being written.
- Native cloud-to-disk backups copy the validated payload file directly instead of reconstructing a whole in-memory blob.
- Decompression is bounded to 1 MiB output pieces and writes records into a spillable store.
- Paste planning groups a configurable number of records at a time and writes the rotated plan to a spillable store.
- Undo and redo before/after records use the same spill-to-disk system.
- Workspace capacity is checked before a large save or load begins.
- Orphaned temporary files from an unclean shutdown are removed on the next startup.

The native NSCM format, MySQL schema, Bedrock add-on, and existing cloud rows remain compatible.

## Validation

The v1.6.1 release passes 65 automated tests, including the large-paste safety regressions plus packet-safe file upload/download and native disk-copy tests that reject whole-file reads. A synthetic 1,000,000-record test uses a 16,000,000-byte file-backed record stream while Python's traced peak remains about 2.6 MiB through record creation, streaming compression, and streaming decode. A streaming 90-degree paste plan for the same record count peaks around 2.2 MiB of traced Python memory.

## Compatibility

- Endstone API 0.11
- Existing MySQL rows remain readable
- No database migration
- No Bedrock add-on changes
- No NSCM format change
- Existing packet-safe MySQL and missing-block policies remain enabled
