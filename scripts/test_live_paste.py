"""Run paste/readback/undo probes on an isolated extracted BDS server, never production."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True, help="Disposable extracted BDS folder below a scratch directory")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    server, output = args.server.resolve(), args.output.resolve()
    if "scratch" not in server.parts or not server.is_dir():
        parser.error("Server must be an existing disposable directory below scratch")
    if output.exists():
        parser.error("Use a new output directory to preserve previous evidence")
    if any((server / "plugins").glob("*.whl")) or any((server / "plugins").glob("*.dll")) or any((server / "plugins").glob("*.so")):
        parser.error("Use a server without installed plugins")
    if any(p.is_file() for p in (server / "plugins" / ".local").rglob("*")):
        parser.error("Use a clean server without a previous Python plugin installation")
    root = Path(__file__).resolve().parents[1]
    output.mkdir(parents=True)
    probe = output / "probe"
    probe.mkdir()
    with ZipFile(args.wheel) as wheel:
        for info in wheel.infolist():
            if not info.filename.startswith("endstone_ninjos_schematics/") or info.is_dir():
                continue
            target = (probe / info.filename).resolve()
            if not target.is_relative_to(probe):
                parser.error("Unsafe wheel member")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(wheel.read(info))
    shutil.copy2(root / "tests/live/paste_probe.py", probe / "probe_plugin.py")
    for dependency in ("tomlkit", "pymysql"):
        spec = importlib.util.find_spec(dependency)
        if spec is None or not spec.submodule_search_locations:
            parser.error(f"Install {dependency} in the test environment first")
        shutil.copytree(next(iter(spec.submodule_search_locations)), probe / dependency, ignore=shutil.ignore_patterns("__pycache__"))
    dist = probe / "endstone_schem_probe-1.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: endstone-schem-probe\nVersion: 1.0.0\n")
    (dist / "entry_points.txt").write_text("[endstone]\nschem-probe = probe_plugin:Probe\n")
    (probe / "sitecustomize.py").write_text(
        'import os,sys\nsys.path[:]=[p for p in sys.path if "site-packages" not in p.lower() or p.lower().startswith(os.environ["SCHEM_PROBE_PREFIX"].lower())]\n'
    )
    (server / "plugins").mkdir(exist_ok=True)
    (server / "server.properties").write_text(
        "server-name=Schematic isolated paste test\nserver-port=39411\nserver-portv6=39412\n"
        "online-mode=true\nallow-list=true\nallow-cheats=true\nlevel-name=schem-test\nlevel-type=FLAT\n"
        "view-distance=4\ntick-distance=4\nenable-lan-visibility=false\nemit-server-telemetry=false\n"
    )
    (server / "endstone.toml").write_text("[settings]\n")
    if os.name == "nt":
        from endstone.cli.windows import WindowsBootstrap, PopenWithDll
        boot = WindowsBootstrap(str(server), True, "", False)
    else:
        from endstone.cli.linux import LinuxBootstrap
        boot = LinuxBootstrap(str(server), True, "", False)
    env = boot._endstone_runtime_env
    env["PYTHONPATH"] = str(probe) + os.pathsep + env["PYTHONPATH"]
    env["SCHEM_PROBE_PREFIX"] = sys.prefix
    env["PYTHONNOUSERSITE"] = "1"
    result = output / "results.json"
    env["SCHEM_PROBE_OUTPUT"] = str(result)
    common = dict(cwd=server, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if os.name == "nt":
        process = PopenWithDll([str(boot.executable_path)], **common, dll_names=str(boot._endstone_runtime_path), creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process = subprocess.Popen([str(boot.executable_path)], **common)
    lines = []

    def reader():
        for line in process.stdout:
            lines.append(line)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline and not result.exists() and process.poll() is None:
            time.sleep(0.5)
        if not result.exists():
            raise RuntimeError(f"Probe did not finish; inspect {output / 'server.log'}")
    finally:
        if process.poll() is None:
            process.stdin.write("stop\n")
            process.stdin.flush()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        thread.join(timeout=5)
        (output / "server.log").write_text("".join(lines), encoding="utf-8")
    report = json.loads(result.read_text())
    report["wheel_sha256"] = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
    report["server_exit"] = process.returncode
    report["platform"] = sys.platform
    result.write_text(json.dumps(report, indent=2) + "\n")
    checks = report["checks"]
    passed = len(checks) == 10 and all(all(c.get(k) is True for k in ("passed", "unchanged_skipped", "undo_passed", "redo_passed")) for c in checks)
    print(f"Live paste/unchanged/undo/redo: {'PASS' if passed else 'FAIL'}; {result}")
    if not passed or process.returncode != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
