# Release Notes: v1.8.1

Fixes false `paste verification stopped ... write verification failed` errors when Endstone resolves an older block name or supplies default states absent from a saved palette. For example, `minecraft:grass` resolves to `minecraft:grass_block`, and stairs/chests receive their current complete state maps. The old verifier could reject these correctly placed blocks.

The verifier now compares the world block with Endstone's resolved target. That target is cached per palette entry and reused for unchanged checks and retries. Saved palettes are not rewritten. Ignored writes and wrong resolved orientations still fail; chunk-residency checks, time/change budgets and partial undo remain active. Failure details are split into shorter chat messages, with the complete comparison retained in the server console.

## Upgrade

If an earlier paste stopped and partial undo is available, use `/schem undo` before shutting down when you need to restore the destination. Undo history does not survive a restart.

Stop Endstone, remove the older schematic wheel, install `endstone_ninjos_schematics-1.8.1-py3-none-any.whl`, and restart. `/schem version` should report build `canonical-paste-verification-20260920`.

Keep existing saves, categories, configuration and add-on packs. This patch adds no database schema changes. Keep `verify_paste_writes = true` and `max_paste_failures = 0`; disabling verification can hide genuinely incomplete pastes.

## Validation

All **142 automated tests passed**, including six real MariaDB integration cases. An isolated Windows Endstone 0.11.11 / BDS 1.26.51.1 run reproduced eight false failures across ten representative block cases before the fix. The packaged v1.8.1 wheel passed all ten cases, including placement, unchanged-block skipping, undo and redo.

See the [validation record](https://github.com/TheNINJALLO/endstone-schematic-cloud/blob/main/docs/validation-1.8.1.md) for reproducible checks, wheel hashes and exact scope. This does not prove every retail-client/custom-pack interaction or diagnose the exact block in a truncated player report. If a paste still fails, capture the complete expected/actual error from the server console.
