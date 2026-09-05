"""Build clean source and installation release archives with SHA-256 checksums."""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = "1.6.1"
PROJECT = f"NinjOS-Schematic-Cloud-{VERSION}"

CORE_ARTIFACTS = [
    DIST / "endstone_ninjos_schematics-1.6.1-py3-none-any.whl",
    DIST / "NinjOS_Schematic_Tools.mcaddon",
    DIST / "NinjOS_Schematics_BP.mcpack",
    DIST / "NinjOS_Schematics_RP.mcpack",
]

SOURCE_ROOTS = [
    "src",
    "tests",
    "scripts",
    "database",
    "addon",
    "docs",
]
SOURCE_FILES = [
    "pyproject.toml",
    "README.md",
    "INSTALL.md",
    "RELEASE_NOTES.md",
    "CHANGELOG.md",
    "LICENSE",
]


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def add_tree(archive: zipfile.ZipFile, source: Path, prefix: Path) -> None:
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if (
            "__pycache__" in path.parts
            or any(part.endswith(".egg-info") for part in path.parts)
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        archive.write(path, (prefix / path.relative_to(ROOT)).as_posix())


def main() -> None:
    DIST.mkdir(exist_ok=True)
    missing = [str(path) for path in CORE_ARTIFACTS if not path.is_file()]
    if missing:
        raise SystemExit(f"Missing built artifacts: {', '.join(missing)}")

    sums_path = DIST / "SHA256SUMS.txt"
    sums_path.write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in CORE_ARTIFACTS),
        encoding="utf-8",
    )

    source_zip = DIST / f"{PROJECT}-source.zip"
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        prefix = Path(PROJECT)
        for root in SOURCE_ROOTS:
            add_tree(archive, ROOT / root, prefix)
        for filename in SOURCE_FILES:
            archive.write(ROOT / filename, (prefix / filename).as_posix())

    release_zip = DIST / f"{PROJECT}-release.zip"
    staging = DIST / f".{PROJECT}-release"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    for path in CORE_ARTIFACTS:
        shutil.copy2(path, staging / path.name)
    for filename in ("README.md", "INSTALL.md", "RELEASE_NOTES.md", "CHANGELOG.md", "LICENSE"):
        shutil.copy2(ROOT / filename, staging / filename)
    shutil.copy2(sums_path, staging / sums_path.name)
    shutil.copytree(ROOT / "database", staging / "database")
    shutil.copytree(ROOT / "docs", staging / "docs")
    shutil.copytree(ROOT / "scripts", staging / "scripts")
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(staging).as_posix())
    shutil.rmtree(staging)

    complete = CORE_ARTIFACTS + [source_zip, release_zip]
    (DIST / "SHA256SUMS-ALL.txt").write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in complete),
        encoding="utf-8",
    )
    print(f"Built {source_zip.name} and {release_zip.name}")


if __name__ == "__main__":
    main()
