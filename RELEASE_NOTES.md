# Release Notes: v1.8.2

Fixes a reproduced false paste failure where `minecraft:flowing_lava {'liquid_depth': 0}` reads back as `minecraft:lava {'liquid_depth': 0}`. The same issue affects flowing water and other liquid depths. Registry resolution alone did not fix this because `create_block_data` preserves the flowing name while the placed world block uses the still name.

Paste verification now treats only the vanilla lava/flowing-lava and water/flowing-water name pairs as equivalent, while comparing their complete states exactly. This comparison also applies to unchanged-block checks and retries. Wrong depths, wrong materials, custom namespaces and ignored writes still fail. Saved palettes remain unchanged; undo/redo retains the actual world block identifiers and states.

## Upgrade

If an earlier paste stopped and partial undo is available, use `/schem undo` before shutting down when you need to restore the destination. Undo history does not survive a restart.

Stop Endstone, remove older schematic wheels, install `endstone_ninjos_schematics-1.8.2-py3-none-any.whl`, and restart. `/schem version` should report build `fluid-paste-verification-20261003`.

Keep existing saves, categories, configuration and add-on packs. This patch adds no database schema changes. Keep `verify_paste_writes = true` and `max_paste_failures = 0`; disabling verification can hide genuinely incomplete pastes.

## Validation

**226 automated tests passed**; six database integration cases were skipped because no disposable database was configured. The 90 new liquid regressions cover both alias directions, every depth, retries, unchanged checks, undo/redo and strict failures. An isolated Windows Endstone 0.11.11 / BDS 1.26.51.1 run reproduced 32 false flowing-liquid failures in v1.8.1. The packaged v1.8.2 wheel passed all **74 real-server cases**, including placement, unchanged-block skipping, undo and redo.

See the [validation record](https://github.com/TheNINJALLO/endstone-schematic-cloud/blob/v1.8.2/docs/validation-1.8.2.md) for reproduction commands, wheel hashes and scope. The player's complete schematic and production world were not part of the isolated run.
