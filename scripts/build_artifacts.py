"""Build mcpack/mcaddon archives from the add-on source folders."""

from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ADDON = ROOT / "addon"
DIST.mkdir(exist_ok=True)


def zip_folder(source: Path, destination: Path, include_root: bool = False) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in source.rglob("*"):
            if path.is_file():
                relative = path.relative_to(source.parent if include_root else source)
                archive.write(path, relative.as_posix())


zip_folder(ADDON / "NinjOS_Schematics_BP", DIST / "NinjOS_Schematics_BP.mcpack")
zip_folder(ADDON / "NinjOS_Schematics_RP", DIST / "NinjOS_Schematics_RP.mcpack")
with zipfile.ZipFile(DIST / "NinjOS_Schematic_Tools.mcaddon", "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for pack_name in ("NinjOS_Schematics_BP", "NinjOS_Schematics_RP"):
        pack = ADDON / pack_name
        for path in pack.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(ADDON).as_posix())
print(f"Built add-on artifacts in {DIST}")
