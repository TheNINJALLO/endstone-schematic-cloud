# Release Notes: v1.7.3

## BlockData detection after startup

Schematic Cloud previously checked for BlockData only once during enable. If the native provider registered its service afterward, retention remained unavailable until a restart. The native `blockdata_api` plugin is now an optional load dependency. Failed detection retries on the next server tick, then every 100 ticks while no save or paste is active. This avoids changing metadata handling midway through an operation. Repeated identical failures do not spam the log.

Missing-service diagnostics now identify whether the native plugin is missing, disabled, or loaded without its service, and include the successfully imported Python API version. A registered provider without block-entity NBT support reports its adapter. Successful detection logs the installed API version and adapter, and `/schem status` changes to Ready.

## Version compatibility

Schematic Cloud uses the installed compatible BlockData package. It does not pin a BlockData release number or download the latest version. `endstone:blockdata:v2` is the service ABI name, not the package release version; Schematic Cloud's `api_version = "0.11"` refers to Endstone.

Install the native BlockData plugin and matching Python bridge from one release bundle compatible with the running BDS and Endstone versions. A missing-service error alone does not prove a version mismatch. Retries recover delayed registration; they cannot install a missing native plugin or repair a binary that failed to enable. Check the native startup log if detection remains unavailable.

## Upgrade and validation

Stop the server, replace the old schematic wheel with `endstone_ninjos_schematics-1.7.3-py3-none-any.whl`, and restart fully. Keep existing configuration, plugin data, database, and add-on packs. `/schem version` should show build `blockdata-startup-retry-20260913`.

All 112 automated tests pass, including delayed registration recovery, bounded retry frequency, active-operation deferral, native-provider diagnostics, and compatibility without a release-number pin. Tests use simulated providers; connection to the affected live server has not been verified. This release addresses BlockData detection and does not establish resolution of slow pastes or client crashes. The v1.7.2 paste scheduler and adaptive pacing improvements are retained.
