"""Atomic on-disk backups for Ninj-OS cloud schematic payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .codec import decode_schematic
from .database import normalize_schematic_name

_EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")


class DiskStoreError(RuntimeError):
    """A local schematic storage operation failed."""


class DiskSchematicExists(DiskStoreError):
    """The destination schematic file already exists."""


class DiskSchematicNotFound(DiskStoreError):
    """The requested local schematic file was not found."""


@dataclass(frozen=True, slots=True)
class DiskSettings:
    enabled: bool
    directory: Path
    extension: str
    auto_create_directory: bool
    write_metadata_sidecar: bool
    overwrite_cloud_exports: bool
    max_file_bytes: int

    @classmethod
    def from_config(cls, config: dict[str, Any], data_folder: str | Path) -> "DiskSettings":
        section = config.get("disk", {})
        raw_directory = str(
            os.environ.get("NINJOS_SCHEM_DISK_DIRECTORY", section.get("directory", "schematics"))
        ).strip()
        if not raw_directory:
            raw_directory = "schematics"
        directory = Path(raw_directory).expanduser()
        if not directory.is_absolute():
            directory = Path(data_folder) / directory
        extension = str(section.get("extension", ".nscm")).strip().lower()
        if not extension.startswith("."):
            extension = f".{extension}"
        if not _EXTENSION_RE.fullmatch(extension):
            raise DiskStoreError("disk.extension must be a dot followed by 1-12 letters or numbers")
        max_mb = max(1, int(section.get("max_file_size_mb", 512)))
        return cls(
            enabled=bool(section.get("enabled", True)),
            directory=directory.resolve(strict=False),
            extension=extension,
            auto_create_directory=bool(section.get("auto_create_directory", True)),
            write_metadata_sidecar=bool(section.get("write_metadata_sidecar", True)),
            overwrite_cloud_exports=bool(section.get("overwrite_cloud_exports", True)),
            max_file_bytes=max_mb * 1024 * 1024,
        )


class DiskSchematicStore:
    def __init__(self, settings: DiskSettings):
        self.settings = settings
        if settings.enabled and settings.auto_create_directory:
            self.ensure_directory()

    @property
    def root(self) -> Path:
        return self.settings.directory

    def ensure_directory(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DiskStoreError(f"unable to create disk schematic directory '{self.root}': {exc}") from exc
        if not self.root.is_dir():
            raise DiskStoreError(f"disk schematic path is not a directory: {self.root}")

    def schematic_path(self, name: str) -> Path:
        normalized = normalize_schematic_name(name)
        return self.root / f"{normalized}{self.settings.extension}"

    def metadata_path(self, name: str) -> Path:
        path = self.schematic_path(name)
        return path.with_name(f"{path.name}.json")

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, bytes):
            return None
        return value

    def save_cloud_row(self, row: dict[str, Any], *, overwrite: bool | None = None) -> Path:
        if not self.settings.enabled:
            raise DiskStoreError("disk schematic storage is disabled in config.toml")
        self.ensure_directory()
        name = normalize_schematic_name(str(row["name"]))
        destination = self.schematic_path(name)
        allow_overwrite = self.settings.overwrite_cloud_exports if overwrite is None else bool(overwrite)
        if destination.exists() and not allow_overwrite:
            raise DiskSchematicExists(f"disk schematic '{destination.name}' already exists")

        payload = bytes(row["payload"])
        if len(payload) > self.settings.max_file_bytes:
            raise DiskStoreError(
                f"schematic payload is {len(payload):,} bytes; disk.max_file_size_mb allows "
                f"{self.settings.max_file_bytes:,} bytes"
            )
        expected_hash = str(row.get("content_sha256", "")).strip() or None
        decoded = decode_schematic(payload, expected_hash)
        actual_hash = hashlib.sha256(payload).hexdigest()

        temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            with temp.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise DiskStoreError(f"unable to write '{destination}': {exc}") from exc

        if self.settings.write_metadata_sidecar:
            metadata = {
                key: self._json_value(value)
                for key, value in row.items()
                if key != "payload" and self._json_value(value) is not None
            }
            metadata.update(
                {
                    "disk_format": "Ninj-OS NSCM",
                    "disk_filename": destination.name,
                    "content_sha256": actual_hash,
                    "size": list(decoded.size),
                    "block_count": decoded.block_count,
                }
            )
            metadata_destination = self.metadata_path(name)
            metadata_temp = metadata_destination.with_name(
                f".{metadata_destination.name}.{os.getpid()}.tmp"
            )
            try:
                with metadata_temp.open("w", encoding="utf-8") as stream:
                    json.dump(metadata, stream, indent=2, ensure_ascii=False, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(metadata_temp, metadata_destination)
            except OSError as exc:
                try:
                    metadata_temp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise DiskStoreError(
                    f"schematic file was saved, but metadata sidecar could not be written: {exc}"
                ) from exc
        return destination


    def save_cloud_file(
        self,
        row: dict[str, Any],
        payload_path: str | Path,
        *,
        overwrite: bool | None = None,
    ) -> Path:
        """Atomically save a validated cloud payload file without loading it into RAM."""

        if not self.settings.enabled:
            raise DiskStoreError("disk schematic storage is disabled in config.toml")
        self.ensure_directory()
        name = normalize_schematic_name(str(row["name"]))
        destination = self.schematic_path(name)
        allow_overwrite = self.settings.overwrite_cloud_exports if overwrite is None else bool(overwrite)
        if destination.exists() and not allow_overwrite:
            raise DiskSchematicExists(f"disk schematic '{destination.name}' already exists")

        source = Path(payload_path)
        if not source.is_file():
            raise DiskStoreError(f"cloud payload file does not exist: {source}")
        payload_bytes = int(source.stat().st_size)
        if payload_bytes > self.settings.max_file_bytes:
            raise DiskStoreError(
                f"schematic payload is {payload_bytes:,} bytes; disk.max_file_size_mb allows "
                f"{self.settings.max_file_bytes:,} bytes"
            )
        expected_bytes = int(row.get("compressed_bytes", payload_bytes))
        if payload_bytes != expected_bytes:
            raise DiskStoreError(
                f"cloud payload length mismatch: expected {expected_bytes:,}, got {payload_bytes:,}"
            )
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for piece in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(piece)
        actual_hash = digest.hexdigest()
        expected_hash = str(row.get("content_sha256", "")).strip().lower()
        if expected_hash and actual_hash.lower() != expected_hash:
            raise DiskStoreError("cloud payload checksum mismatch before disk export")

        temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            with source.open("rb") as source_stream, temp.open("wb") as destination_stream:
                shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
            os.replace(temp, destination)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise DiskStoreError(f"unable to write '{destination}': {exc}") from exc

        if self.settings.write_metadata_sidecar:
            metadata = {
                key: self._json_value(value)
                for key, value in row.items()
                if key not in {"payload", "payload_path"} and self._json_value(value) is not None
            }
            metadata.update(
                {
                    "disk_format": "Ninj-OS NSCM",
                    "disk_filename": destination.name,
                    "content_sha256": actual_hash,
                    "size": [
                        int(row.get("size_x", 0)),
                        int(row.get("size_y", 0)),
                        int(row.get("size_z", 0)),
                    ],
                    "block_count": int(row.get("block_count", 0)),
                }
            )
            metadata_destination = self.metadata_path(name)
            metadata_temp = metadata_destination.with_name(
                f".{metadata_destination.name}.{os.getpid()}.tmp"
            )
            try:
                with metadata_temp.open("w", encoding="utf-8") as stream:
                    json.dump(metadata, stream, indent=2, ensure_ascii=False, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(metadata_temp, metadata_destination)
            except OSError as exc:
                metadata_temp.unlink(missing_ok=True)
                raise DiskStoreError(
                    f"schematic file was saved, but metadata sidecar could not be written: {exc}"
                ) from exc
        return destination

    def read(self, name: str) -> tuple[bytes, dict[str, Any]]:
        path = self.schematic_path(name)
        if not path.is_file():
            raise DiskSchematicNotFound(f"disk schematic '{path.name}' was not found")
        try:
            size = path.stat().st_size
            if size > self.settings.max_file_bytes:
                raise DiskStoreError(
                    f"disk schematic is {size:,} bytes; configured maximum is {self.settings.max_file_bytes:,}"
                )
            payload = path.read_bytes()
        except OSError as exc:
            raise DiskStoreError(f"unable to read '{path}': {exc}") from exc

        metadata: dict[str, Any] = {}
        sidecar = self.metadata_path(name)
        if sidecar.is_file():
            try:
                loaded = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    metadata = loaded
            except (OSError, json.JSONDecodeError) as exc:
                raise DiskStoreError(f"unable to read metadata sidecar '{sidecar}': {exc}") from exc
        expected_hash = str(metadata.get("content_sha256", "")).strip() or None
        decoded = decode_schematic(payload, expected_hash)
        metadata.setdefault("name", normalize_schematic_name(name))
        metadata.setdefault("display_name", decoded.header.get("display_name", metadata["name"]))
        metadata.setdefault("description", decoded.header.get("description", ""))
        metadata.setdefault("author_name", decoded.header.get("author_name", "Unknown"))
        metadata.setdefault("source_server", decoded.header.get("source_server", "Unknown"))
        metadata.setdefault("source_dimension", decoded.header.get("source_dimension", ""))
        metadata.setdefault("size_x", decoded.size[0])
        metadata.setdefault("size_y", decoded.size[1])
        metadata.setdefault("size_z", decoded.size[2])
        metadata.setdefault("block_count", decoded.block_count)
        metadata.setdefault("content_sha256", hashlib.sha256(payload).hexdigest())
        metadata["payload"] = payload
        return payload, metadata

    def list(self, search: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if not self.settings.enabled:
            return []
        self.ensure_directory()
        needle = search.strip().casefold()
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob(f"*{self.settings.extension}"), key=lambda p: p.stat().st_mtime, reverse=True):
            name = path.name[: -len(self.settings.extension)]
            if needle and needle not in name.casefold():
                sidecar_text = ""
                sidecar = path.with_name(f"{path.name}.json")
                if sidecar.is_file():
                    try:
                        sidecar_text = sidecar.read_text(encoding="utf-8").casefold()
                    except OSError:
                        pass
                if needle not in sidecar_text:
                    continue
            rows.append(
                {
                    "name": name,
                    "filename": path.name,
                    "bytes": path.stat().st_size,
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                }
            )
            if len(rows) >= max(1, min(500, int(limit))):
                break
        return rows

    def delete(self, name: str) -> None:
        path = self.schematic_path(name)
        if not path.exists():
            raise DiskSchematicNotFound(f"disk schematic '{path.name}' was not found")
        try:
            path.unlink()
            self.metadata_path(name).unlink(missing_ok=True)
        except OSError as exc:
            raise DiskStoreError(f"unable to delete disk schematic '{path.name}': {exc}") from exc
