# Release Notes: v1.7.2

## Slow and stalled pastes

On legacy Endstone builds, checking chunk residency enumerates the dimension's loaded chunks. When that check took longer than the 10 ms paste budget, v1.7.1 could yield before placing any blocks and repeat the same check forever. A confirmed resident chunk now permits one record before yielding. Positively verified, plugin-held paste chunks reuse their residency confirmation for up to 10 ticks, with a fresh check at completion. Missing/unknown chunks, native checks, and source scans do not use this cache.

Already resident chunks skip stabilization after a native hold is acquired and verified. Newly generated chunks and legacy ticking-area acquisition retain their loading and stabilization waits.

## Adaptive throughput

```toml
[performance]
paste_blocks_per_tick = 1200
paste_changed_blocks_per_tick = 1200
paste_adaptive_pacing = true
paste_time_budget_ms = 10
chunk_loads_per_tick = 1
```

Pacing starts at 256 changes per tick and increases after healthy server ticks, up to the configured limits. Slow ticks above 75 ms halve the current budget, down to 64 or the configured maximum if lower. Catch-up callbacks do not trigger acceleration. The shared record and time limits continue to apply; a single native call cannot be interrupted.

The shipped v1.7.1 value of 256 is automatically migrated to 1,200. Other administrator-selected limits are preserved. Setting `paste_adaptive_pacing = false` retains a fixed configured ceiling, including 256.

`/schem status` now shows actual records/sec, processed/total records, last chunk-check and placement times, total chunk wait ticks, and current/maximum change limits. An unavailable BlockData service does not prevent ordinary block pastes.

## Upgrade and validation

Stop the server, replace the old schematic wheel with `endstone_ninjos_schematics-1.7.2-py3-none-any.whl`, and restart fully. Keep existing configuration, plugin data, database, and add-on packs. `/schem version` should show build `paste-progress-budget-20260913`.

All 101 automated tests pass. A controlled simulation with a 20 ms legacy chunk check reproduced 0 of 4,096 records placed over 100 ticks on v1.7.1. The updated scheduler completed all 4,096 records in 18 ticks with three chunk enumerations. This establishes the scheduler regression fix; it does not measure live BDS throughput or confirm resolution of client crashes.

NSCM v1/v2, block-state verification, metadata restoration, and undo/redo compatibility are retained.
