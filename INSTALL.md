# Ninj-OS Schematic Cloud v1.8.1 Installation and Upgrade

## Clean wheel upgrade

1. Stop the server completely. Do not use `/reload`.
2. Remove every older `endstone_ninjos_schematics-*.whl` from the top-level `plugins` directory.
3. Remove the cached package and matching `.dist-info` directories under `plugins/.local/lib/python3.14/site-packages/`, or run `scripts/purge_stale_install.sh` while the server is stopped.
4. Upload only:

```text
endstone_ninjos_schematics-1.8.1-py3-none-any.whl
```

5. Keep the existing plugin data folder, database, and `config.toml`.
6. Start the server and run:

```text
/schem version
/schem dbtest
/schem status
```

The startup log must contain:

```text
Enabled v1.8.1 build=canonical-paste-verification-20260920
```

This upgrade adds shared storage categories and stronger save/paste chunk checks. With `database.auto_create_schema = true`, startup creates two category tables without changing existing saves or payloads. If automatic schema creation is disabled, run the updated `database/schema.sql` first, adjusting every table prefix to match the configuration. Existing entries appear in Uncategorized. Update all servers that share the library before using category features. Existing add-on packs remain compatible.

Existing configurations receive `performance.scan_time_budget_ms = 5`. The old adaptive default of 1,200 changed blocks per tick is lowered to 256 to reduce client update bursts. Other custom limits and limits with adaptive pacing disabled are preserved. Keep the 10 ms paste budget. `/schem status` shows scan destination and limits as well as paste throughput, timings, and chunk waits. Unknown chunk residency now stops progress until the chunk is positively confirmed loaded.

## v1.8.1 paste-verification fix

This patch accepts the destination registry's canonical block names and complete state maps, including default states missing from older palettes. It retains strict readback, chunk-residency checks and partial undo on genuine failures. Keep `verify_paste_writes = true` and `max_paste_failures = 0`. Existing cloud saves, categories, configuration and add-on packs remain compatible; no additional schema migration is introduced by this patch.

If an earlier paste stopped, restore its available partial undo before repeating the paste when practical. Failure details now appear in bounded chat lines and remain complete in the server console. [Validation record](docs/validation-1.8.1.md).

## Optional BlockData retention

To preserve supported block-entity NBT and container inventories, install the native plugin and matching platform-specific CPython bridge from one [`endstone-blockdata`](https://github.com/TheNINJALLO/endstone-blockdata-api/releases) release bundle. That bundle must match the running BDS and Endstone versions exactly.

Schematic Cloud detects the installed package; it does not download updates or require a fixed BlockData release number. `endstone:blockdata:v2` is the service ABI name, separate from the package version. Installing only the Python API/bridge is insufficient: the native `blockdata_api` plugin must enable and register that service.

The native plugin is an optional load dependency. Failed detection retries on the next server tick and then every 100 ticks, deferred while saves or pastes are active. The unavailable-service error now distinguishes a missing native plugin, a disabled plugin, and a plugin with no registered service. Check the native startup log if the error persists.

After a full restart, startup should report the BlockData API version and adapter. Confirm in game:

```text
/schem status
```

The generated configuration receives:

```toml
[blockdata]
enabled = true
strict_restore = true
max_uncompressed_mb = 64
```

`strict_restore = true` stops a paste when saved metadata cannot be restored and preserves partial undo history. The size limit prevents an unusually metadata-heavy selection from consuming unbounded memory; raise it only when the server has enough headroom.

Existing NSCM v1 rows remain readable. v1.8.1 creates NSCM v2 payloads, so update all schematic-cloud servers before they consume newly saved entries.

## Automatically merged streaming settings

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

A relative `temp_directory` is created below the plugin data folder. An absolute path can place temporary work on a larger or faster volume:

```toml
[streaming]
temp_directory = "/home/container/schematic_streaming_work"
```

The directory may temporarily contain raw save records, downloaded compressed payloads, decoded records, rotated paste plans, and undo/redo records. Files are removed as soon as their operation or history entry is released.

## Capacity planning

A full-volume schematic record uses 16 bytes per block. The streaming pipeline keeps that bulk on disk instead of multiplying it in RAM, but the temporary volume still needs enough free space.

Approximate upper bounds:

- Save: roughly 32 bytes per selected block plus 64 MiB safety room.
- Load and paste without undo: roughly 32 bytes per stored record plus the compressed payload.
- Load and paste with undo: roughly 64 bytes per stored record plus the compressed payload.

For 16,000,000 stored records, allow at least 1 to 2 GiB of free temporary space. The plugin refuses to begin when the configured workspace cap or free-disk reserve would be exceeded.

## Recommended large-build settings

```toml
[performance]
max_blocks_per_schematic = 20000000
scan_blocks_per_tick = 2500
paste_blocks_per_tick = 1200
paste_time_budget_ms = 10
worker_threads = 2

[history]
enabled = true
max_blocks_per_operation = 2000000
max_total_blocks_per_player = 2000000
```

Increasing `max_blocks_per_schematic` permits a larger volume. It does not require raising the scan or paste budgets. `paste_time_budget_ms` is a wall-clock safety cap; the paste yields when either it or `paste_blocks_per_tick` is reached. Keeping both limits moderate protects Bedrock's main server thread.

Undo is automatically disabled for one paste when its planned record count is above `history.max_blocks_per_operation`. Raise that limit only when the temporary disk has room for both before and after records.

## Existing safeguards retained

Keep these enabled:

```toml
[performance]
auto_load_missing_chunks = true
chunk_load_timeout_ticks = 1200
chunk_stabilize_ticks = 4
max_chunk_retries = 3
verify_paste_writes = true
max_paste_failures = 0

[placement]
missing_block_policy = "skip"
```

Packet-safe MySQL payload rows, retry handling, chunk residency verification, missing custom-block handling, disk backups, WorldEdit/Amulet export, rotation, confirmation, particles, and operator-or-`architect` access remain compatible.

## Add-on

The Bedrock add-on did not change. Upgrading from v1.1.0 or newer requires replacing the schematic plugin wheel; BlockData retention additionally requires its exact native plugin and bridge bundle.
