"""Remote MySQL storage for shared schematics.

Large schematic payloads are split into packet-safe rows. This avoids relying on a
host's ``max_allowed_packet`` value and lets a long upload retry cleanly without
rescanning the Minecraft world.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - reported cleanly by plugin startup
    pymysql = None
    DictCursor = None

_PREFIX_RE = re.compile(r"^[A-Za-z0-9_]{0,24}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TRANSIENT_MYSQL_CODES = {
    1040,  # Too many connections
    1042,  # Unable to connect to host
    1047,  # Unknown command / server restarting
    1158,
    1159,
    1160,
    1161,
    1205,  # Lock wait timeout
    1213,  # Deadlock
    2002,
    2003,
    2006,  # Server has gone away
    2013,  # Lost connection during query
    2026,  # SSL connection error
}


class DatabaseError(RuntimeError):
    """Database operation failed."""


class SchematicAlreadyExists(DatabaseError):
    """A schematic with this namespace/name already exists."""


class SchematicNotFound(DatabaseError):
    """The requested schematic does not exist."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    namespace: str
    table_prefix: str
    connect_timeout: int
    read_timeout: int
    write_timeout: int
    auto_create_schema: bool
    ssl_ca: str
    payload_chunk_bytes: int
    inline_payload_max_bytes: int
    retry_attempts: int
    retry_backoff_seconds: float

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DatabaseSettings":
        section = config.get("database", {})
        env = os.environ
        prefix = str(section.get("table_prefix", "ninjos_"))
        if not _PREFIX_RE.fullmatch(prefix):
            raise DatabaseError("database.table_prefix may only contain letters, numbers, and underscores")
        namespace = str(env.get("NINJOS_SCHEM_NAMESPACE", section.get("namespace", "global"))).strip()
        if not namespace or len(namespace) > 64:
            raise DatabaseError("database.namespace must be 1-64 characters")

        chunk_mb = max(1, min(15, int(section.get("payload_chunk_size_mb", 2))))
        inline_mb = max(0, min(chunk_mb, int(section.get("inline_payload_max_mb", 2))))
        return cls(
            host=str(env.get("NINJOS_SCHEM_DB_HOST", section.get("host", "127.0.0.1"))),
            port=int(env.get("NINJOS_SCHEM_DB_PORT", section.get("port", 3306))),
            user=str(env.get("NINJOS_SCHEM_DB_USER", section.get("user", "schematics"))),
            password=str(env.get("NINJOS_SCHEM_DB_PASSWORD", section.get("password", ""))),
            database=str(env.get("NINJOS_SCHEM_DB_NAME", section.get("database", "ninjos_schematics"))),
            namespace=namespace,
            table_prefix=prefix,
            connect_timeout=max(1, int(section.get("connect_timeout_seconds", 10))),
            read_timeout=max(1, int(section.get("read_timeout_seconds", 120))),
            write_timeout=max(1, int(section.get("write_timeout_seconds", 120))),
            auto_create_schema=bool(section.get("auto_create_schema", True)),
            ssl_ca=str(env.get("NINJOS_SCHEM_DB_SSL_CA", section.get("ssl_ca", ""))).strip(),
            payload_chunk_bytes=chunk_mb * 1024 * 1024,
            inline_payload_max_bytes=inline_mb * 1024 * 1024,
            retry_attempts=max(1, min(10, int(section.get("retry_attempts", 3)))),
            retry_backoff_seconds=max(
                0.1, min(30.0, float(section.get("retry_backoff_seconds", 2.0)))
            ),
        )


def normalize_schematic_name(name: str) -> str:
    normalized = name.strip().lower().replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9._-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-._")
    if not _NAME_RE.fullmatch(normalized):
        raise ValueError("schematic name must become 1-64 characters using a-z, 0-9, dot, underscore, or dash")
    return normalized


def iter_payload_chunks(payload: bytes, chunk_bytes: int) -> Iterator[bytes]:
    """Yield non-empty packet-safe payload slices."""

    size = max(1, int(chunk_bytes))
    for offset in range(0, len(payload), size):
        yield payload[offset : offset + size]


def iter_file_chunks(path: Path, chunk_bytes: int) -> Iterator[bytes]:
    """Yield packet-safe chunks from a file without loading it whole."""

    size = max(1, int(chunk_bytes))
    with Path(path).open("rb") as stream:
        while True:
            piece = stream.read(size)
            if not piece:
                break
            yield piece


def assemble_payload_chunks(
    rows: Iterable[dict[str, Any]],
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> tuple[bytes, int]:
    """Validate and combine ordered payload chunk rows."""

    payload = bytearray()
    expected_index = 0
    count = 0
    for row in rows:
        index = int(row.get("chunk_index", -1))
        if index != expected_index:
            raise DatabaseError(
                f"schematic payload chunks are incomplete: expected index {expected_index}, got {index}"
            )
        piece = bytes(row.get("payload") or b"")
        declared_bytes = int(row.get("chunk_bytes", -1))
        if declared_bytes != len(piece):
            raise DatabaseError(
                f"schematic payload chunk {index} length mismatch: expected {declared_bytes}, got {len(piece)}"
            )
        declared_hash = str(row.get("chunk_sha256", "")).lower()
        actual_hash = hashlib.sha256(piece).hexdigest()
        if declared_hash != actual_hash:
            raise DatabaseError(f"schematic payload chunk {index} checksum mismatch")
        payload.extend(piece)
        expected_index += 1
        count += 1

    if not count and expected_bytes:
        raise DatabaseError("schematic payload is empty and no payload chunks were found")
    if len(payload) != int(expected_bytes):
        raise DatabaseError(
            f"schematic payload length mismatch: expected {int(expected_bytes):,}, got {len(payload):,}"
        )
    actual_total_hash = hashlib.sha256(payload).hexdigest()
    if expected_sha256 and actual_total_hash.lower() != str(expected_sha256).lower():
        raise DatabaseError("assembled schematic payload checksum mismatch")
    return bytes(payload), count


class MySQLSchematicStore:
    def __init__(self, settings: DatabaseSettings):
        if pymysql is None:
            raise DatabaseError(
                "PyMySQL is not installed. Install the wheel dependencies in Endstone's Python environment."
            )
        self.settings = settings
        self.table = f"{settings.table_prefix}schematics"
        self.chunk_table = f"{settings.table_prefix}schematic_payload_chunks"

    @contextmanager
    def _connection(self, *, autocommit: bool = True) -> Iterator[Any]:
        ssl = {"ca": self.settings.ssl_ca} if self.settings.ssl_ca else None
        try:
            connection = pymysql.connect(
                host=self.settings.host,
                port=self.settings.port,
                user=self.settings.user,
                password=self.settings.password,
                database=self.settings.database,
                charset="utf8mb4",
                autocommit=autocommit,
                connect_timeout=self.settings.connect_timeout,
                read_timeout=self.settings.read_timeout,
                write_timeout=self.settings.write_timeout,
                cursorclass=DictCursor,
                ssl=ssl,
            )
        except Exception as exc:
            raise DatabaseError(f"unable to connect to MySQL: {exc}") from exc
        try:
            yield connection
        finally:
            try:
                connection.close()
            except Exception:
                pass

    @staticmethod
    def _mysql_error_code(exc: BaseException) -> int | None:
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            args = getattr(current, "args", ())
            if args and isinstance(args[0], int):
                return int(args[0])
            current = current.__cause__ or current.__context__
        return None

    @classmethod
    def _is_transient_error(cls, exc: BaseException) -> bool:
        code = cls._mysql_error_code(exc)
        if code in _TRANSIENT_MYSQL_CODES:
            return True
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "server has gone away",
                "lost connection",
                "connection reset",
                "broken pipe",
                "timed out",
                "timeout",
                "connection refused",
            )
        )

    def ping(self) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                row = cursor.fetchone()
                if not row or row.get("ok") != 1:
                    raise DatabaseError("MySQL health check returned an unexpected result")

    def ensure_schema(self) -> None:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self.table}` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            `namespace` VARCHAR(64) NOT NULL,
            `name` VARCHAR(64) NOT NULL,
            `display_name` VARCHAR(128) NOT NULL,
            `description` TEXT NOT NULL,
            `author_uuid` VARCHAR(64) NOT NULL,
            `author_xuid` VARCHAR(32) NOT NULL,
            `author_name` VARCHAR(64) NOT NULL,
            `source_server` VARCHAR(96) NOT NULL,
            `source_dimension` VARCHAR(64) NOT NULL,
            `minecraft_version` VARCHAR(32) NOT NULL,
            `plugin_version` VARCHAR(32) NOT NULL,
            `format_version` SMALLINT UNSIGNED NOT NULL,
            `size_x` INT UNSIGNED NOT NULL,
            `size_y` INT UNSIGNED NOT NULL,
            `size_z` INT UNSIGNED NOT NULL,
            `block_count` BIGINT UNSIGNED NOT NULL,
            `non_air_count` BIGINT UNSIGNED NOT NULL,
            `palette_count` INT UNSIGNED NOT NULL,
            `includes_air` TINYINT(1) NOT NULL,
            `content_sha256` CHAR(64) NOT NULL,
            `compressed_bytes` BIGINT UNSIGNED NOT NULL,
            `uncompressed_bytes` BIGINT UNSIGNED NOT NULL,
            `payload` LONGBLOB NOT NULL,
            `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            `updated_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            `deleted_at` TIMESTAMP(6) NULL DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uq_namespace_name` (`namespace`, `name`),
            KEY `idx_namespace_updated` (`namespace`, `updated_at`),
            KEY `idx_namespace_deleted` (`namespace`, `deleted_at`),
            KEY `idx_hash` (`content_sha256`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        chunk_ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self.chunk_table}` (
            `schematic_id` BIGINT UNSIGNED NOT NULL,
            `chunk_index` INT UNSIGNED NOT NULL,
            `chunk_bytes` INT UNSIGNED NOT NULL,
            `chunk_sha256` CHAR(64) NOT NULL,
            `payload` MEDIUMBLOB NOT NULL,
            PRIMARY KEY (`schematic_id`, `chunk_index`),
            KEY `idx_schematic_id` (`schematic_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(ddl)
                cursor.execute(chunk_ddl)

    @staticmethod
    def _row_columns() -> tuple[str, ...]:
        return (
            "namespace", "name", "display_name", "description", "author_uuid", "author_xuid",
            "author_name", "source_server", "source_dimension", "minecraft_version", "plugin_version",
            "format_version", "size_x", "size_y", "size_z", "block_count", "non_air_count",
            "palette_count", "includes_air", "content_sha256", "compressed_bytes",
            "uncompressed_bytes", "payload",
        )

    def _save_once(self, row: dict[str, Any], overwrite: bool) -> dict[str, Any]:
        payload = bytes(row["payload"])
        use_chunks = len(payload) > self.settings.inline_payload_max_bytes
        stored_row = dict(row)
        stored_row["payload"] = b"" if use_chunks else payload

        columns = self._row_columns()
        values = [stored_row[column] for column in columns]
        quoted_columns = ", ".join(f"`{column}`" for column in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        if overwrite:
            updates = ", ".join(
                f"`{column}`=VALUES(`{column}`)"
                for column in columns
                if column not in {"namespace", "name"}
            )
            # LAST_INSERT_ID(id) makes cursor.lastrowid return the existing row ID too.
            updates += ", `deleted_at`=NULL, `id`=LAST_INSERT_ID(`id`)"
            sql = (
                f"INSERT INTO `{self.table}` ({quoted_columns}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {updates}"
            )
        else:
            sql = f"INSERT INTO `{self.table}` ({quoted_columns}) VALUES ({placeholders})"

        connection = None
        try:
            with self._connection(autocommit=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, values)
                    schematic_id = int(getattr(cursor, "lastrowid", 0) or 0)
                    if schematic_id <= 0:
                        cursor.execute(
                            f"SELECT `id` FROM `{self.table}` WHERE `namespace`=%s AND `name`=%s LIMIT 1",
                            (row["namespace"], row["name"]),
                        )
                        found = cursor.fetchone()
                        if not found:
                            raise DatabaseError("unable to resolve the saved schematic row ID")
                        schematic_id = int(found["id"])

                    cursor.execute(
                        f"DELETE FROM `{self.chunk_table}` WHERE `schematic_id`=%s",
                        (schematic_id,),
                    )

                    chunk_count = 0
                    if use_chunks:
                        chunk_sql = (
                            f"INSERT INTO `{self.chunk_table}` "
                            "(`schematic_id`, `chunk_index`, `chunk_bytes`, `chunk_sha256`, `payload`) "
                            "VALUES (%s, %s, %s, %s, %s)"
                        )
                        for chunk_index, piece in enumerate(
                            iter_payload_chunks(payload, self.settings.payload_chunk_bytes)
                        ):
                            cursor.execute(
                                chunk_sql,
                                (
                                    schematic_id,
                                    chunk_index,
                                    len(piece),
                                    hashlib.sha256(piece).hexdigest(),
                                    piece,
                                ),
                            )
                            chunk_count += 1
                        cursor.execute(
                            f"SELECT COUNT(*) AS `count`, COALESCE(SUM(`chunk_bytes`), 0) AS `bytes` "
                            f"FROM `{self.chunk_table}` WHERE `schematic_id`=%s",
                            (schematic_id,),
                        )
                        verification = cursor.fetchone() or {}
                        if int(verification.get("count", -1)) != chunk_count:
                            raise DatabaseError("database payload chunk count did not verify after upload")
                        if int(verification.get("bytes", -1)) != len(payload):
                            raise DatabaseError("database payload byte count did not verify after upload")
                connection.commit()
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise

        return {
            "storage": "chunked" if use_chunks else "inline",
            "chunk_count": chunk_count if use_chunks else 0,
            "chunk_bytes": self.settings.payload_chunk_bytes if use_chunks else 0,
        }

    def _save_file_once(
        self, row: dict[str, Any], payload_path: Path, overwrite: bool
    ) -> dict[str, Any]:
        payload_path = Path(payload_path)
        payload_bytes = int(payload_path.stat().st_size)
        if payload_bytes != int(row.get("compressed_bytes", payload_bytes)):
            raise DatabaseError("payload file length does not match row metadata")
        use_chunks = payload_bytes > self.settings.inline_payload_max_bytes
        stored_row = dict(row)
        stored_row["payload"] = b"" if use_chunks else payload_path.read_bytes()

        columns = self._row_columns()
        values = [stored_row[column] for column in columns]
        quoted_columns = ", ".join(f"`{column}`" for column in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        if overwrite:
            updates = ", ".join(
                f"`{column}`=VALUES(`{column}`)"
                for column in columns
                if column not in {"namespace", "name"}
            )
            updates += ", `deleted_at`=NULL, `id`=LAST_INSERT_ID(`id`)"
            sql = (
                f"INSERT INTO `{self.table}` ({quoted_columns}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {updates}"
            )
        else:
            sql = f"INSERT INTO `{self.table}` ({quoted_columns}) VALUES ({placeholders})"

        connection = None
        try:
            with self._connection(autocommit=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, values)
                    schematic_id = int(getattr(cursor, "lastrowid", 0) or 0)
                    if schematic_id <= 0:
                        cursor.execute(
                            f"SELECT `id` FROM `{self.table}` WHERE `namespace`=%s AND `name`=%s LIMIT 1",
                            (row["namespace"], row["name"]),
                        )
                        found = cursor.fetchone()
                        if not found:
                            raise DatabaseError("unable to resolve the saved schematic row ID")
                        schematic_id = int(found["id"])
                    cursor.execute(
                        f"DELETE FROM `{self.chunk_table}` WHERE `schematic_id`=%s",
                        (schematic_id,),
                    )
                    chunk_count = 0
                    if use_chunks:
                        chunk_sql = (
                            f"INSERT INTO `{self.chunk_table}` "
                            "(`schematic_id`, `chunk_index`, `chunk_bytes`, `chunk_sha256`, `payload`) "
                            "VALUES (%s, %s, %s, %s, %s)"
                        )
                        for chunk_index, piece in enumerate(
                            iter_file_chunks(payload_path, self.settings.payload_chunk_bytes)
                        ):
                            cursor.execute(
                                chunk_sql,
                                (
                                    schematic_id,
                                    chunk_index,
                                    len(piece),
                                    hashlib.sha256(piece).hexdigest(),
                                    piece,
                                ),
                            )
                            chunk_count += 1
                        cursor.execute(
                            f"SELECT COUNT(*) AS `count`, COALESCE(SUM(`chunk_bytes`), 0) AS `bytes` "
                            f"FROM `{self.chunk_table}` WHERE `schematic_id`=%s",
                            (schematic_id,),
                        )
                        verification = cursor.fetchone() or {}
                        if int(verification.get("count", -1)) != chunk_count:
                            raise DatabaseError("database payload chunk count did not verify after upload")
                        if int(verification.get("bytes", -1)) != payload_bytes:
                            raise DatabaseError("database payload byte count did not verify after upload")
                connection.commit()
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise
        return {
            "storage": "chunked" if use_chunks else "inline",
            "chunk_count": chunk_count if use_chunks else 0,
            "chunk_bytes": self.settings.payload_chunk_bytes if use_chunks else 0,
        }

    def _existing_matching_receipt(self, name: str, expected_sha256: str) -> dict[str, Any] | None:
        """Recognize a commit whose acknowledgement was lost before a retry."""

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT `id`, `content_sha256`, OCTET_LENGTH(`payload`) AS `inline_bytes` "
                    f"FROM `{self.table}` WHERE `namespace`=%s AND `name`=%s "
                    "AND `deleted_at` IS NULL LIMIT 1",
                    (self.settings.namespace, normalize_schematic_name(name)),
                )
                row = cursor.fetchone()
                if not row or str(row.get("content_sha256", "")).lower() != expected_sha256.lower():
                    return None
                if int(row.get("inline_bytes", 0)) > 0:
                    return {"storage": "inline", "chunk_count": 0, "chunk_bytes": 0}
                cursor.execute(
                    f"SELECT COUNT(*) AS `count` FROM `{self.chunk_table}` WHERE `schematic_id`=%s",
                    (int(row["id"]),),
                )
                count_row = cursor.fetchone() or {}
                return {
                    "storage": "chunked",
                    "chunk_count": int(count_row.get("count", 0)),
                    "chunk_bytes": self.settings.payload_chunk_bytes,
                }

    def save(self, row: dict[str, Any], overwrite: bool) -> dict[str, Any]:
        last_error: BaseException | None = None
        for attempt in range(1, self.settings.retry_attempts + 1):
            try:
                return self._save_once(row, overwrite)
            except Exception as exc:
                if self._mysql_error_code(exc) == 1062:
                    if attempt > 1:
                        try:
                            receipt = self._existing_matching_receipt(
                                str(row["name"]), str(row["content_sha256"])
                            )
                        except Exception:
                            receipt = None
                        if receipt is not None:
                            receipt["recovered_commit"] = True
                            return receipt
                    raise SchematicAlreadyExists(
                        f"schematic '{row['name']}' already exists; enable overwrite to replace it"
                    ) from exc
                last_error = exc
                if attempt >= self.settings.retry_attempts or not self._is_transient_error(exc):
                    break
                time.sleep(self.settings.retry_backoff_seconds * (2 ** (attempt - 1)))
        raise DatabaseError(
            f"unable to save schematic after {self.settings.retry_attempts} attempt(s): {last_error}"
        ) from last_error

    def save_file(
        self, row: dict[str, Any], payload_path: Path, overwrite: bool
    ) -> dict[str, Any]:
        """Save a compressed payload from disk with retry-safe chunk streaming."""

        last_error: BaseException | None = None
        for attempt in range(1, self.settings.retry_attempts + 1):
            try:
                return self._save_file_once(row, payload_path, overwrite)
            except Exception as exc:
                if self._mysql_error_code(exc) == 1062:
                    if attempt > 1:
                        try:
                            receipt = self._existing_matching_receipt(
                                str(row["name"]), str(row["content_sha256"])
                            )
                        except Exception:
                            receipt = None
                        if receipt is not None:
                            receipt["recovered_commit"] = True
                            return receipt
                    raise SchematicAlreadyExists(
                        f"schematic '{row['name']}' already exists; enable overwrite to replace it"
                    ) from exc
                last_error = exc
                if attempt >= self.settings.retry_attempts or not self._is_transient_error(exc):
                    break
                time.sleep(self.settings.retry_backoff_seconds * (2 ** (attempt - 1)))
        raise DatabaseError(
            f"unable to save schematic after {self.settings.retry_attempts} attempt(s): {last_error}"
        ) from last_error

    def fetch_to_file(self, name: str, destination: Path) -> dict[str, Any]:
        """Download and validate one payload into a file with bounded memory."""

        normalized = normalize_schematic_name(name)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        sql = (
            f"SELECT * FROM `{self.table}` WHERE `namespace`=%s AND `name`=%s "
            "AND `deleted_at` IS NULL LIMIT 1"
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, (self.settings.namespace, normalized))
                row = cursor.fetchone()
                if not row:
                    raise SchematicNotFound(f"schematic '{name}' was not found")
                digest = hashlib.sha256()
                total = 0
                chunk_count = 0
                inline_payload = bytes(row.get("payload") or b"")
                with destination.open("wb") as stream:
                    if inline_payload:
                        stream.write(inline_payload)
                        digest.update(inline_payload)
                        total = len(inline_payload)
                        row["payload_storage"] = "inline"
                    else:
                        cursor.execute(
                            f"SELECT COUNT(*) AS `count` FROM `{self.chunk_table}` WHERE `schematic_id`=%s",
                            (int(row["id"]),),
                        )
                        count_row = cursor.fetchone() or {}
                        expected_count = int(count_row.get("count", 0))
                        for index in range(expected_count):
                            cursor.execute(
                                f"SELECT `chunk_index`, `chunk_bytes`, `chunk_sha256`, `payload` "
                                f"FROM `{self.chunk_table}` WHERE `schematic_id`=%s AND `chunk_index`=%s LIMIT 1",
                                (int(row["id"]), index),
                            )
                            chunk_row = cursor.fetchone()
                            if not chunk_row or int(chunk_row.get("chunk_index", -1)) != index:
                                raise DatabaseError(
                                    f"schematic payload chunks are incomplete at index {index}"
                                )
                            piece = bytes(chunk_row.get("payload") or b"")
                            if len(piece) != int(chunk_row.get("chunk_bytes", -1)):
                                raise DatabaseError(f"schematic payload chunk {index} length mismatch")
                            if hashlib.sha256(piece).hexdigest().lower() != str(
                                chunk_row.get("chunk_sha256", "")
                            ).lower():
                                raise DatabaseError(f"schematic payload chunk {index} checksum mismatch")
                            stream.write(piece)
                            digest.update(piece)
                            total += len(piece)
                            chunk_count += 1
                        row["payload_storage"] = "chunked"
                if total != int(row.get("compressed_bytes", total)):
                    destination.unlink(missing_ok=True)
                    raise DatabaseError(
                        f"schematic payload length mismatch: expected {int(row.get('compressed_bytes', 0)):,}, got {total:,}"
                    )
                if digest.hexdigest().lower() != str(row.get("content_sha256", "")).lower():
                    destination.unlink(missing_ok=True)
                    raise DatabaseError("downloaded schematic payload checksum mismatch")
                row.pop("payload", None)
                row["payload_path"] = destination
                row["payload_chunk_count"] = chunk_count
                return row

    def fetch(self, name: str) -> dict[str, Any]:
        sql = (
            f"SELECT * FROM `{self.table}` WHERE `namespace`=%s AND `name`=%s "
            "AND `deleted_at` IS NULL LIMIT 1"
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, (self.settings.namespace, normalize_schematic_name(name)))
                row = cursor.fetchone()
                if not row:
                    raise SchematicNotFound(f"schematic '{name}' was not found")

                inline_payload = bytes(row.get("payload") or b"")
                if inline_payload:
                    if len(inline_payload) != int(row.get("compressed_bytes", len(inline_payload))):
                        raise DatabaseError("inline schematic payload length does not match its metadata")
                    actual_hash = hashlib.sha256(inline_payload).hexdigest()
                    if actual_hash.lower() != str(row.get("content_sha256", "")).lower():
                        raise DatabaseError("inline schematic payload checksum mismatch")
                    row["payload"] = inline_payload
                    row["payload_storage"] = "inline"
                    row["payload_chunk_count"] = 0
                    return row

                cursor.execute(
                    f"SELECT `chunk_index`, `chunk_bytes`, `chunk_sha256`, `payload` "
                    f"FROM `{self.chunk_table}` WHERE `schematic_id`=%s ORDER BY `chunk_index` ASC",
                    (int(row["id"]),),
                )
                payload, chunk_count = assemble_payload_chunks(
                    cursor.fetchall(),
                    expected_bytes=int(row.get("compressed_bytes", 0)),
                    expected_sha256=str(row.get("content_sha256", "")),
                )
                row["payload"] = payload
                row["payload_storage"] = "chunked"
                row["payload_chunk_count"] = chunk_count
                return row

    def list(self, search: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        base_columns = (
            "`name`, `display_name`, `description`, `author_name`, `source_server`, "
            "`size_x`, `size_y`, `size_z`, `block_count`, `non_air_count`, `palette_count`, "
            "`includes_air`, `compressed_bytes`, `created_at`, `updated_at`"
        )
        params: list[Any] = [self.settings.namespace]
        where = "`namespace`=%s AND `deleted_at` IS NULL"
        if search.strip():
            where += " AND (`name` LIKE %s OR `display_name` LIKE %s OR `description` LIKE %s)"
            pattern = f"%{search.strip()}%"
            params.extend((pattern, pattern, pattern))
        params.extend((limit, offset))
        sql = (
            f"SELECT {base_columns} FROM `{self.table}` WHERE {where} "
            "ORDER BY `updated_at` DESC LIMIT %s OFFSET %s"
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def hard_delete(self, name: str) -> None:
        """Permanently remove a schematic row and any packet-safe payload chunks."""

        normalized = normalize_schematic_name(name)
        connection = None
        try:
            with self._connection(autocommit=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT `id` FROM `{self.table}` WHERE `namespace`=%s AND `name`=%s LIMIT 1",
                        (self.settings.namespace, normalized),
                    )
                    row = cursor.fetchone()
                    if not row:
                        raise SchematicNotFound(f"schematic '{name}' was not found")
                    cursor.execute(
                        f"DELETE FROM `{self.chunk_table}` WHERE `schematic_id`=%s",
                        (int(row["id"]),),
                    )
                    affected = cursor.execute(
                        f"DELETE FROM `{self.table}` WHERE `id`=%s",
                        (int(row["id"]),),
                    )
                    if affected == 0:
                        raise SchematicNotFound(f"schematic '{name}' was not found")
                connection.commit()
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise

    def soft_delete(self, name: str) -> None:
        sql = (
            f"UPDATE `{self.table}` SET `deleted_at`=CURRENT_TIMESTAMP(6) "
            "WHERE `namespace`=%s AND `name`=%s AND `deleted_at` IS NULL"
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                affected = cursor.execute(sql, (self.settings.namespace, normalize_schematic_name(name)))
        if affected == 0:
            raise SchematicNotFound(f"schematic '{name}' was not found")
