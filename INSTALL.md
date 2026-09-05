# Ninj-OS Schematic Cloud v1.6.1 Installation and Upgrade

## Clean wheel upgrade

1. Stop the server completely. Do not use `/reload`.
2. Remove every older `endstone_ninjos_schematics-*.whl` from the top-level `plugins` directory.
3. Remove the cached package and matching `.dist-info` directories under `plugins/.local/lib/python3.14/site-packages/`, or run `scripts/purge_stale_install.sh` while the server is stopped.
4. Upload only:

```text
endstone_ninjos_schematics-1.6.1-py3-none-any.whl
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
Enabled v1.6.1 build=large-paste-watchdog-safety-20260904
```

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

A full-volume schematic record uses 16 bytes per block. v1.6.0 keeps that bulk on disk instead of multiplying it in RAM, but the temporary volume still needs enough free space.

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

The Bedrock add-on did not change. Upgrading from v1.1.0 or newer only requires replacing the plugin wheel.
