# Release Notes: v1.8.0

## Storage categories

Use **Storage Categories** in `/schem menu` to create and browse categories. Saving asks for a destination; library items have **Move to Category**. Both lists support pagination. Commands are also available:

```text
/schem category create castles
/schem save castle-gate true false castles
/schem move castle-gate castles
/schem move castle-gate uncategorized
```

Existing saves appear in Uncategorized. Moves change only the shared category assignment; payload bytes and names stay intact. Names remain unique per namespace across categories. Overwrites without an explicit destination preserve the current category. Category assignment and new payload writes use the same transaction.

## Scan stalls and incomplete regions

Save scans now share a 5 ms elapsed-time budget, rotate between builders, and split BlockData captures into small native calls. The default paste change ceiling is 256 instead of 1,200 to reduce client update bursts even when server ticks are healthy.

Unknown residency no longer authorizes reads or writes. A held source chunk that drops between ticks has its partial scan discarded and retried. A partially written destination chunk that loses verified residency stops instead of reporting successful completion; available undo history is preserved. Plugin chunk tickets are preferred when supported. Shared native holds remain until every owning job releases them. Legacy source checks reuse positive held residency briefly and always check again at completion.

## Upgrade

Stop the server, replace older wheels with `endstone_ninjos_schematics-1.8.0-py3-none-any.whl`, and restart. Keep plugin data and existing add-on packs. `/schem version` should show build `categories-chunk-safety-20260919`.

Automatic schema setup creates two additional category tables without rewriting existing saves. With `auto_create_schema = false`, apply the updated `database/schema.sql` first using the configured prefix. Upgrade every server sharing the library. Configuration merging adds the scan budget and changes the previous adaptive 1,200-change default to 256; other custom limits and explicitly fixed pacing remain intact.

## Validation and limits

All 134 tests passed, including six integration tests against an isolated MariaDB 11.4 instance. Coverage includes category forms, complete delayed-chunk scans and pastes, unknown residency, shared holds, scan yielding, disconnects, metadata, undo/redo, and exports. Database tests verify schema upgrades, namespace isolation, pagination, moves, payload preservation, overwrites, and rollback on invalid destinations.

The native ticket and asynchronous residency handling follow the [Endstone Dimension API](https://endstone.dev/latest/reference/python/level/#endstone.level.Dimension).

Live BDS/client timeouts still require an on-server verification with the affected builds. A time budget cannot interrupt one native API call already in progress. Previously saved missing sections cannot be reconstructed from absent data; resave affected builds from the original world after upgrading.
