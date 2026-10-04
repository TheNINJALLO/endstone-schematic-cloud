# v1.8.2 liquid paste verification validation

Validated on 2026-10-03 using Windows Endstone 0.11.11 / BDS 1.26.51.1.

## Reproduced failure and fix

The reported paste stopped because the resolved target was `minecraft:flowing_lava` with `liquid_depth=0`, but the world returned `minecraft:lava` with the same depth. Unlike the registry aliases fixed in v1.8.1, `create_block_data` preserves the flowing name in this case. Normalization occurs when the block is placed/read back.

The v1.8.1 wheel failed all 32 flowing-liquid cases in the probe: flowing lava and flowing water, each at depths 0 through 15. The ten existing representative block cases and 32 still-liquid cases passed. Requested, resolved and actual data appear in [the reproduction report](validation/1.8.1-liquid-windows-reproduction.json).

The fix compares vanilla lava/flowing-lava and water/flowing-water names as equivalent while retaining exact state maps. Unchanged checks, placement readback and immediate retry use the same comparison. The source palette and cached resolved data stay intact; history records actual world names and states. Wrong materials, wrong depths and ignored writes still fail.

## Results

- **226 automated tests passed; six database integration cases skipped.** No disposable database was configured, and this patch changes no database code or schema.
- **90 new liquid regressions** cover both alias directions, every depth, unchanged checks without writes or history capture, retry, actual-state undo/redo, wrong depths with partial undo, wrong liquids, custom namespaces and ignored writes.
- The **built v1.8.2 wheel passed all 74 real-server cases**, each checking placement, unchanged-block skipping, undo and redo. The cases include the previous ten representative blocks plus both names of lava and water at every depth.
- Both the baseline and final server runs shut down cleanly with exit code zero. The final report records the wheel and plugin-source SHA-256 hashes: [v1.8.2 wheel results](validation/1.8.2-liquid-windows-wheel.json).
- Wheel package files matched the source bytes; metadata and the Endstone entry point were verified. Ruff passed for changed Python code with the existing `E731` lambda in `plugin.py` excluded. `git diff --check` passed.

## Reproduce

Automated tests require pytest, PyMySQL and tomlkit. From the project root:

```powershell
$env:PYTHONPATH = 'src'
python -m pytest -q
python -m build --wheel
```

For the live probe, use an isolated Python environment containing Endstone 0.11.11, PyMySQL and tomlkit, with matching official BDS files in a disposable directory below `scratch`. The directory must have no installed plugins:

```powershell
python scripts/test_live_paste.py --server build/scratch/server --wheel dist/endstone_ninjos_schematics-1.8.2-py3-none-any.whl --output build/scratch/liquid-wheel-check
```

The runner creates a fresh test world for every run and contains each liquid sample with stone so scheduled flow or water/lava reactions cannot contaminate another case. It preserves existing output and test worlds, uses ports 39411/39412, extracts the plugin code from the wheel and loads a probe subclass without cloud/database services. It stops the disposable server after completion. Running the same command with the v1.8.1 wheel and a different output directory reproduces the 32 flowing-liquid failures.

## Scope

The live probe uses real server block APIs and the packaged paste/history methods. It does not load the player's complete `pvp-spawn-1-3` schematic, modify the production server, connect a retail client, validate arbitrary delayed physics or test custom behavior packs. Verification remains enabled; liquid reactions that produce another material are genuine mismatches and still stop the paste.
