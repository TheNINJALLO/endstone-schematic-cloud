# v1.8.1 paste verification validation

Validated on 2026-09-20. This patch addresses a reproduced false `write verification failed` result caused by destination-registry normalization. It does not disable verification or change chunk admission, write pacing, storage categories or the database schema.

## Reproduced failure

An isolated Windows Endstone 0.11.11 / BDS 1.26.51.1 server ran the v1.8.0 paste method against ten block cases. Eight stopped even though the written block exactly matched the data returned by `server.create_block_data`. Examples:

| Saved palette | Destination registry result |
| --- | --- |
| `minecraft:oak_stairs` with no states | Default orientation, upside-down flag and `minecraft:corner` state |
| `minecraft:chest` with no states | Chest with default cardinal direction |
| `minecraft:grass` | `minecraft:grass_block` |
| `minecraft:stone` with `stone_type=granite` | `minecraft:granite` |

The old verifier compared the written block with the original dictionary/name. v1.8.1 instead compares it with the resolved target, cached per palette entry. Saved palettes remain unchanged. A genuinely ignored write or incorrect resolved orientation still fails.

The [Endstone BlockData API](https://endstone.dev/latest/reference/python/block/#endstone.block.BlockData) exposes the resolved type and states. The real-server before/after records below provide the specific normalization evidence; the truncated player report does not identify which block caused that player's failure.

## Results

- **142 automated tests passed**, including six integration cases against an isolated MariaDB 11.4 instance. No database cases were skipped in the final run.
- Eight new regressions cover default states, aliases, unchanged blocks, reuse of resolved data for retries, partial undo on wrong orientation, and bounded error messages. Six of the initial seven regressions failed before the fix; the wrong-orientation rejection already passed.
- The **built v1.8.1 wheel** passed all ten real-server cases: stone; default and partially specified stairs; chest; legacy grass; door; water; torch; horizontal oak log; and legacy granite. Every case verified placement, unchanged-block skipping, undo to air and redo to the resolved state.
- The isolated server shut down cleanly. The final wheel SHA-256 is recorded in [the wheel results](validation/1.8.1-windows-wheel.json); the published checksums identify the release packages.
- Source lint checks passed for the new files and changed models. The existing `E731` lambda in `plugin.py` is excluded from that file's lint check; it predates this patch.

[v1.8.0 reproduction](validation/1.8.0-windows-reproduction.json) and [v1.8.1 wheel results](validation/1.8.1-windows-wheel.json) contain the requested, resolved and actual block states.

## Reproduce

Use a source checkout or the source release archive. Automated tests need pytest, PyMySQL and tomlkit; set `PYTHONPATH=src`. Set `SCHEM_TEST_DB_PORT` only to a disposable loopback MariaDB/MySQL instance with the `schem_test` database and test root/no-password access. The database tests create unique tables and remove those tables after each case.

```powershell
$env:PYTHONPATH = 'src'
$env:SCHEM_TEST_DB_PORT = '33418'
python -m pytest -q
```

For the live probe, use an isolated Python environment with Endstone 0.11.11, PyMySQL and tomlkit. Extract the matching official BDS server into a disposable folder below a directory named `scratch`. Use a server with no installed plugins, not production data:

```powershell
python scripts/test_live_paste.py --server build/scratch/server --wheel dist/endstone_ninjos_schematics-1.8.1-py3-none-any.whl --output build/scratch/wheel-check
```

The runner overwrites that disposable server's properties, uses ports 39411/39412 and a separate flat test world, extracts only the plugin code from the wheel, loads a probe subclass instead of starting cloud/database services, and stops the server after completion. It refuses to overwrite existing output. Results include wheel/source hashes and a server log. The runtime environment should contain no unrelated Endstone plugins.

## Limits

The live run validates the packaged paste and history methods using real server block APIs. It does not connect a retail client, reproduce this player's complete schematic, exercise custom behavior packs or validate optional BlockData inventory restoration. Existing mocked chunk-loss and metadata regressions remain in the suite. This is not a new measurement of client timeout behavior or every block's delayed physics. If another paste stops, retain the complete server-console expected/actual error and the output of `/schem version`.
