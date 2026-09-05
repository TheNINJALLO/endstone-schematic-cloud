"""Main Endstone API 0.11 plugin implementation."""

from __future__ import annotations

import json
import math
import queue
import shlex
import shutil
import tempfile
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from time import monotonic
from typing import Any, Callable

import tomlkit
from endstone import Player
from endstone.command import Command, CommandSender
from endstone.inventory import ItemStack
from endstone.plugin import Plugin

from .access import player_has_schematic_access
from .blockdata_integration import BlockDataIntegration, BlockDataIntegrationError
from .codec import (
    FORMAT_VERSION,
    RECORD,
    append_record,
    decode_schematic,
    decode_schematic_file,
    encode_schematic_to_file,
    palette_key,
    record_at,
)
from .chunk_loading import (
    chunk_loaded_state,
    ticket_name as build_ticket_name,
    tickingarea_add_command,
    tickingarea_remove_command,
)
from .compat import dimension_identifier as get_dimension_identifier, resolve_dimension
from .config_merge import merge_missing
from .database import (
    DatabaseSettings,
    MySQLSchematicStore,
    normalize_schematic_name,
)
from .disk_store import DiskSchematicStore, DiskSettings
from .forms import SchematicForms
from .listener import SchematicToolListener
from .models import (
    BlockPos,
    HistoryEntry,
    PasteChunkRange,
    PasteJob,
    PastePlan,
    PlacementSession,
    SaveJob,
    Selection,
)
from .planner import build_chunk_regions, prepare_paste_plan, prepare_streaming_paste_plan, validate_schematic_integrity
from .record_store import RecordSource, SpillRecordBuffer, cleanup_orphan_record_files
from .rotation import normalize_rotation, rotated_size
from .sponge_schem import (
    WorldEditSchematicStore,
    WorldEditSettings,
    encode_sponge_v3,
)

PLUGIN_VERSION = "1.7.0"
BUILD_ID = "blockdata-nscm-v2-20260904"
_ACTIVE_PLUGIN_INSTANCE: Any | None = None
AIR_TYPES = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}


class NinjOSSchematicsPlugin(Plugin):
    """Shared MySQL, native disk, and WorldEdit/Amulet schematic library."""

    prefix = "Ninj-OS Schematics"
    version = PLUGIN_VERSION
    api_version = "0.11"
    authors = ["Ninj-OS"]

    commands = {
        "schem": {
            "description": "Manage Ninj-OS cloud, disk, and WorldEdit/Amulet schematics.",
            "usages": ["/schem [args: message]"],
            "permissions": ["ninjos.schematics.command"],
        }
    }

    permissions = {
        "ninjos.schematics.command": {
            "description": "Route /schem to the operator-or-architect role gate.",
            "default": True,
        },
        "ninjos.schematics.use": {
            "description": "Use schematic selection, preview, and menu tools.",
            "default": "op",
        },
        "ninjos.schematics.save": {
            "description": "Save schematics to the shared database.",
            "default": "op",
        },
        "ninjos.schematics.load": {
            "description": "Load and paste schematics from the shared database.",
            "default": "op",
        },
        "ninjos.schematics.delete": {
            "description": "Remove shared schematics from MySQL.",
            "default": "op",
        },
        "ninjos.schematics.tools": {
            "description": "Receive the schematic tool items.",
            "default": "op",
        },
        "ninjos.schematics.admin": {
            "description": "Run database diagnostics.",
            "default": "op",
        },
    }

    def _merge_config_defaults(self) -> None:
        """Add newly introduced settings without replacing administrator values."""

        try:
            packaged = files(__package__).joinpath("config.toml").read_text(encoding="utf-8")
            defaults = tomlkit.parse(packaged)
            changed = merge_missing(self.config, defaults)
            performance = self.config.get("performance", {})
            # v1.0-v1.3 shipped 200 ticks as the default. That is too short for generated
            # or storage-bound chunks, so migrate only that exact legacy default while
            # preserving any administrator-chosen custom timeout.
            if int(performance.get("chunk_load_timeout_ticks", 1200)) == 200:
                performance["chunk_load_timeout_ticks"] = 1200
                changed = True
            database = self.config.get("database", {})
            # Earlier releases used 30-second socket timeouts. Keep administrator
            # overrides, but migrate those exact old defaults for large remote uploads.
            if int(database.get("connect_timeout_seconds", 10)) == 5:
                database["connect_timeout_seconds"] = 10
                changed = True
            if int(database.get("read_timeout_seconds", 120)) == 30:
                database["read_timeout_seconds"] = 120
                changed = True
            if int(database.get("write_timeout_seconds", 120)) == 30:
                database["write_timeout_seconds"] = 120
                changed = True
            if changed:
                self.save_config()
                self.logger.info("Merged newly introduced settings into config.toml.")
        except Exception as exc:
            self.logger.warning(f"Unable to merge config defaults: {exc}")

    def on_enable(self) -> None:
        global _ACTIVE_PLUGIN_INSTANCE
        existing = _ACTIVE_PLUGIN_INSTANCE
        if existing is not None and existing is not self and not getattr(existing, "_stopping", True):
            self._duplicate_instance = True
            self._stopping = True
            self.logger.error(
                "Duplicate plugin instance blocked. Remove every older Ninj-OS Schematics "
                "wheel and fully restart the server; do not use /reload for wheel upgrades."
            )
            return
        _ACTIVE_PLUGIN_INSTANCE = self
        self._duplicate_instance = False
        self.save_default_config()
        self._merge_config_defaults()
        access = self.config.get("access", {})
        self._architect_tag = str(access.get("architect_tag", "architect")).strip() or "architect"
        self._access_denied_message = str(
            access.get(
                "denied_message",
                "Only server operators and players with the architect tag can use Ninj-OS Schematics.",
            )
        )
        self._stopping = False
        self._tick_counter = 0
        self._completion_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.selections: dict[Any, Selection] = {}
        self.placements: dict[Any, PlacementSession] = {}
        self.save_jobs: dict[Any, SaveJob] = {}
        self.paste_jobs: dict[Any, PasteJob] = {}
        self.preparing_pastes: dict[Any, tuple[object, PlacementSession]] = {}
        self.undo_history: dict[Any, list[HistoryEntry]] = {}
        self.redo_history: dict[Any, list[HistoryEntry]] = {}
        self.db_ready = False
        self.db_error = "Database initialization is still running."

        performance = self.config.get("performance", {})
        workers = max(1, min(8, int(performance.get("worker_threads", 2))))
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ninjos-schem")
        self._scan_budget = max(1, int(performance.get("scan_blocks_per_tick", 2500)))
        self._paste_budget = max(1, int(performance.get("paste_blocks_per_tick", 1200)))
        self._paste_time_budget_seconds = max(
            0.001,
            min(0.045, float(performance.get("paste_time_budget_ms", 10)) / 1000.0),
        )
        self._max_blocks = max(1, int(performance.get("max_blocks_per_schematic", 2_000_000)))
        self._completion_budget = max(1, int(performance.get("completion_callbacks_per_tick", 32)))
        self._compression_level = max(0, min(9, int(performance.get("compression_level", 6))))
        self._apply_physics = bool(performance.get("apply_physics", False))
        self._skip_unchanged = bool(performance.get("skip_unchanged_blocks", True))
        self._auto_load_chunks = bool(performance.get("auto_load_missing_chunks", True))
        self._chunk_load_timeout = max(20, int(performance.get("chunk_load_timeout_ticks", 1200)))
        self._chunk_stabilize_ticks = max(0, int(performance.get("chunk_stabilize_ticks", 4)))
        self._max_chunk_retries = max(0, int(performance.get("max_chunk_retries", 3)))
        self._legacy_tickingarea_fallback = bool(
            performance.get("legacy_tickingarea_fallback", True)
        )
        self._legacy_tickingarea_preload = bool(
            performance.get("legacy_tickingarea_preload", True)
        )
        self._legacy_tickingarea_prefix = str(
            performance.get("legacy_tickingarea_prefix", "njs_schem")
        ).strip() or "njs_schem"
        self._legacy_tickingarea_max_active = max(
            1, min(10, int(performance.get("legacy_tickingarea_max_active", 8)))
        )
        self._legacy_ticket_slots: dict[int, Any] = {}
        self._legacy_ticket_session = uuid.uuid4().hex[:6]
        self._legacy_ticket_registry: dict[str, dict[str, Any]] = {}
        self._stale_legacy_ticket_records = self._load_legacy_ticket_registry()
        # Clear the previous session's journal before scheduling cleanup. New tickets
        # created during this boot are written to a fresh journal and cannot be mistaken
        # for stale tickets by the delayed cleanup task.
        if self._stale_legacy_ticket_records:
            self._write_legacy_ticket_registry({})
        self._verify_paste_writes = bool(performance.get("verify_paste_writes", True))
        self._max_paste_failures = max(0, int(performance.get("max_paste_failures", 0)))
        self._progress_interval = max(10, int(performance.get("progress_message_interval_ticks", 40)))

        streaming = self.config.get("streaming", {})
        configured_work_dir = Path(str(streaming.get("temp_directory", "streaming_work")))
        if not configured_work_dir.is_absolute():
            configured_work_dir = Path(self.data_folder) / configured_work_dir
        self._stream_work_dir = configured_work_dir.resolve()
        self._stream_work_dir.mkdir(parents=True, exist_ok=True)
        self._record_spill_threshold = max(1, int(streaming.get("memory_spill_threshold_mb", 8))) * 1024 * 1024
        self._plan_batch_records = max(1024, int(streaming.get("plan_batch_records", 32768)))
        self._max_stream_work_bytes = max(256, int(streaming.get("max_temp_workspace_mb", 16384))) * 1024 * 1024
        self._min_free_stream_bytes = max(128, int(streaming.get("minimum_free_disk_mb", 1024))) * 1024 * 1024
        self._streaming_enabled = bool(streaming.get("enabled", True))
        removed_orphans = 0
        if bool(streaming.get("cleanup_orphans_on_startup", True)):
            removed_orphans = cleanup_orphan_record_files(self._stream_work_dir)
        if removed_orphans:
            self.logger.info(f"Removed {removed_orphans:,} orphaned streaming workspace file(s).")

        placement = self.config.get("placement", {})
        self._missing_block_policy = str(
            placement.get("missing_block_policy", "skip")
        ).strip().lower()
        if self._missing_block_policy not in {"skip", "air", "fallback", "abort"}:
            self.logger.warning(
                f"Unknown placement.missing_block_policy '{self._missing_block_policy}'; using skip."
            )
            self._missing_block_policy = "skip"
        self._missing_block_fallback = str(
            placement.get("missing_block_fallback", "minecraft:stone")
        ).strip() or "minecraft:stone"
        self._missing_block_report_limit = max(
            1, min(100, int(placement.get("missing_block_report_limit", 20)))
        )

        preview = self.config.get("preview", {})
        self._preview_enabled = bool(preview.get("enabled", True))
        self._preview_particle = str(preview.get("particle", "ninjos:placement_outline"))
        if self._preview_particle == "minecraft:basic_crit_particle":
            # Migrate the 1.0.x default in memory without rewriting the administrator's file.
            self._preview_particle = "ninjos:placement_outline"
        self._preview_points = max(2, min(64, int(preview.get("points_per_edge", 12))))
        self._preview_refresh = max(1, int(preview.get("refresh_ticks", 10)))
        self._preview_duration_ticks = max(20, int(preview.get("duration_seconds", 90)) * 20)
        self._selection_preview_enabled = bool(preview.get("selection_enabled", True))
        self._selection_particle = str(preview.get("selection_particle", "ninjos:selection_outline"))
        self._selection_points = max(2, min(64, int(preview.get("selection_points_per_edge", self._preview_points))))

        history = self.config.get("history", {})
        self._history_enabled = bool(history.get("enabled", True))
        self._history_max_operations = max(1, min(50, int(history.get("max_operations_per_player", 5))))
        self._history_max_blocks_per_operation = max(1, int(history.get("max_blocks_per_operation", self._max_blocks)))
        self._history_max_total_blocks = max(1, int(history.get("max_total_blocks_per_player", self._max_blocks)))

        blockdata = self.config.get("blockdata", {})
        self._blockdata_enabled = bool(blockdata.get("enabled", True))
        self._blockdata_strict_restore = bool(blockdata.get("strict_restore", True))
        self._blockdata_max_bytes = max(
            1, int(blockdata.get("max_uncompressed_mb", 64))
        ) * 1024 * 1024
        self._blockdata: BlockDataIntegration | None = None
        self._blockdata_error = "disabled in config.toml"
        if self._blockdata_enabled:
            try:
                self._blockdata = BlockDataIntegration.connect(self.server)
                self._blockdata_error = ""
                self.logger.info(
                    f"BlockData API v{self._blockdata.api_version} connected through "
                    f"adapter={self._blockdata.adapter_name}; block-entity and container data "
                    "will be retained in native NSCM saves."
                )
            except Exception as exc:
                self._blockdata_error = str(exc)
                self.logger.warning(
                    "BlockData integration is unavailable; base block types and states will "
                    f"still work, but block-entity data will not be retained: {exc}"
                )

        tools = self.config.get("tools", {})
        self.tool_ids = {
            "selector": str(tools.get("selector", "ninjos:schem_selector")),
            "placer": str(tools.get("placer", "ninjos:schem_placer")),
            "rotator": str(tools.get("rotator", "ninjos:schem_rotator")),
            "tablet": str(tools.get("tablet", "ninjos:schem_tablet")),
            "undo": str(tools.get("undo", "ninjos:schem_undo")),
            "redo": str(tools.get("redo", "ninjos:schem_redo")),
            "confirm": str(tools.get("confirm", "ninjos:schem_confirm")),
        }
        debounce_ms = max(50, min(2000, int(tools.get("interaction_debounce_ms", 450))))
        self._tool_debounce_seconds = debounce_ms / 1000.0
        self.server_id = str(self.config.get("server", {}).get("server_id", "server-1"))[:96]
        self._hard_delete_enabled = bool(
            self.config.get("database", {}).get("hard_delete_enabled", True)
        )

        try:
            disk_settings = DiskSettings.from_config(self.config, self.data_folder)
            self.disk_store = DiskSchematicStore(disk_settings)
            self.disk_error = ""
        except Exception as exc:
            self.logger.error(f"Disk storage configuration error: {exc}")
            self.disk_store = None
            self.disk_error = str(exc)

        try:
            worldedit_settings = WorldEditSettings.from_config(self.config, self.data_folder)
            self.worldedit_store = WorldEditSchematicStore(worldedit_settings)
            self.worldedit_error = ""
        except Exception as exc:
            self.logger.error(f"WorldEdit/Amulet export configuration error: {exc}")
            self.worldedit_store = None
            self.worldedit_error = str(exc)

        try:
            settings = DatabaseSettings.from_config(self.config)
            self.store = MySQLSchematicStore(settings)
        except Exception as exc:
            self.logger.error(f"Database configuration error: {exc}")
            self.db_error = str(exc)
            self.store = None
        self.forms = SchematicForms(self)
        self.register_events(SchematicToolListener(self))
        if self._stale_legacy_ticket_records:
            self.server.scheduler.run_task(
                self,
                self._cleanup_stale_legacy_tickets,
                delay=40,
            )
        self.server.scheduler.run_task(self, self._tick, delay=1, period=1)

        if self.store is not None:
            self._submit_worker(
                self._initialize_database,
                self._database_initialized,
                self._database_initialization_failed,
            )
        self.logger.info(
            f"Enabled v{PLUGIN_VERSION} build={BUILD_ID}; scan budget={self._scan_budget}/tick, "
            f"paste budget={self._paste_budget}/tick or "
            f"{self._paste_time_budget_seconds * 1000:g}ms/tick, workers={workers}, "
            f"tool debounce={debounce_ms}ms; access=operator-or-tag:{self._architect_tag}; "
            f"disk={self.disk_store.root if self.disk_store else 'disabled'}; "
            f"worldedit={self.worldedit_store.root if self.worldedit_store else 'disabled'}; "
            f"streaming={'enabled' if self._streaming_enabled else 'disabled'} "
            f"spill={self._record_spill_threshold // (1024**2)}MiB work={self._stream_work_dir}; "
            f"module={Path(__file__).resolve()}."
        )

    def on_disable(self) -> None:
        global _ACTIVE_PLUGIN_INSTANCE
        if getattr(self, "_duplicate_instance", False):
            self.logger.info("Disabled duplicate inert plugin instance.")
            return
        self._stopping = True
        try:
            self.server.scheduler.cancel_tasks(self)
        except Exception:
            pass
        for job in list(getattr(self, "save_jobs", {}).values()):
            self._release_job_chunk(job, release_slot=True)
            self._close_record_storage(getattr(job, "records", None))
        for job in list(getattr(self, "paste_jobs", {}).values()):
            self._release_job_chunk(job, release_slot=True)
            if getattr(job, "operation", "paste") == "paste":
                self._cleanup_plan(getattr(job, "plan", None))
                self._close_record_storage(getattr(job, "before_records", None))
                self._close_record_storage(getattr(job, "after_records", None))
        for pending in list(getattr(self, "preparing_pastes", {}).values()):
            if isinstance(pending, tuple) and len(pending) > 1:
                self._cleanup_placement(pending[1])
        for placement in list(getattr(self, "placements", {}).values()):
            self._cleanup_placement(placement)
        for stack in list(getattr(self, "undo_history", {}).values()):
            self._cleanup_history_stack(stack)
        for stack in list(getattr(self, "redo_history", {}).values()):
            self._cleanup_history_stack(stack)
        executor = getattr(self, "_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        getattr(self, "save_jobs", {}).clear()
        getattr(self, "paste_jobs", {}).clear()
        getattr(self, "preparing_pastes", {}).clear()
        getattr(self, "placements", {}).clear()
        getattr(self, "undo_history", {}).clear()
        getattr(self, "redo_history", {}).clear()
        if _ACTIVE_PLUGIN_INSTANCE is self:
            _ACTIVE_PLUGIN_INSTANCE = None
        self.logger.info("Disabled; active jobs were cancelled.")

    # ------------------------------------------------------------------
    # Worker bridge and database lifecycle
    # ------------------------------------------------------------------

    def _submit_worker(
        self,
        operation: Callable[[], Any],
        success: Callable[[Any], None],
        failure: Callable[[BaseException], None] | None = None,
    ) -> None:
        if self._stopping:
            return
        try:
            future = self._executor.submit(operation)
        except RuntimeError as exc:
            if failure:
                failure(exc)
            return

        def completed(done: Future[Any]) -> None:
            try:
                result = done.result()
            except BaseException as exc:  # worker exceptions must cross to main thread
                callback = (lambda error=exc: failure(error)) if failure else (
                    lambda error=exc: self.logger.error(f"Worker operation failed: {error}")
                )
            else:
                callback = lambda value=result: success(value)
            self._completion_queue.put(callback)

        future.add_done_callback(completed)

    def _initialize_database(self) -> bool:
        assert self.store is not None
        if self.store.settings.auto_create_schema:
            self.store.ensure_schema()
        self.store.ping()
        return True

    def _database_initialized(self, _result: Any) -> None:
        self.db_ready = True
        self.db_error = ""
        assert self.store is not None
        self.logger.info(
            f"Connected to MySQL namespace '{self.store.settings.namespace}' at "
            f"{self.store.settings.host}:{self.store.settings.port}."
        )

    def _database_initialization_failed(self, error: BaseException) -> None:
        self.db_ready = False
        self.db_error = str(error)
        self.logger.error(f"MySQL initialization failed: {error}")

    def _require_database(self, sender: CommandSender) -> bool:
        if self.db_ready and self.store is not None:
            return True
        sender.send_error_message(f"Schematic database is unavailable: {self.db_error}")
        return False

    def _require_disk(self, sender: CommandSender) -> bool:
        if self.disk_store is not None and self.disk_store.settings.enabled:
            return True
        detail = getattr(self, "disk_error", "disk storage is disabled") or "disk storage is disabled"
        sender.send_error_message(f"Schematic disk storage is unavailable: {detail}")
        return False

    def _require_worldedit(self, sender: CommandSender) -> bool:
        if self.worldedit_store is not None and self.worldedit_store.settings.enabled:
            return True
        detail = (
            getattr(self, "worldedit_error", "WorldEdit/Amulet export is disabled")
            or "WorldEdit/Amulet export is disabled"
        )
        sender.send_error_message(f"WorldEdit/Amulet export is unavailable: {detail}")
        return False

    def has_schematic_access(self, player: Player) -> bool:
        """Operators and players tagged architect are the only allowed users."""

        return player_has_schematic_access(player, getattr(self, "_architect_tag", "architect"))

    def require_schematic_access(self, player: Player, *, notify: bool = True) -> bool:
        allowed = self.has_schematic_access(player)
        if not allowed and notify:
            player.send_error_message(
                getattr(
                    self,
                    "_access_denied_message",
                    "Only server operators and players with the architect tag can use Ninj-OS Schematics.",
                )
            )
        return allowed

    # ------------------------------------------------------------------
    # Bounded-memory workspace helpers
    # ------------------------------------------------------------------

    def _new_record_buffer(self, prefix: str) -> SpillRecordBuffer:
        return SpillRecordBuffer(
            self._stream_work_dir,
            threshold_bytes=self._record_spill_threshold,
            prefix=prefix,
        )

    def _new_payload_path(self, prefix: str = "payload-") -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=prefix,
            suffix=".nscm.tmp",
            dir=self._stream_work_dir,
            delete=False,
        )
        path = Path(handle.name)
        handle.close()
        return path

    @staticmethod
    def _close_record_storage(value: Any) -> None:
        close = getattr(value, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _cleanup_decoded_schematic(self, schematic: Any | None) -> None:
        if schematic is not None:
            self._close_record_storage(getattr(schematic, "records", None))

    def _cleanup_placement(self, placement: Any | None) -> None:
        if placement is not None:
            self._cleanup_decoded_schematic(getattr(placement, "schematic", None))

    def _cleanup_plan(self, plan: Any | None) -> None:
        if plan is not None:
            self._close_record_storage(getattr(plan, "records", None))

    def _cleanup_history_entry(self, entry: Any | None) -> None:
        if entry is None:
            return
        self._cleanup_plan(getattr(entry, "before_plan", None))
        self._cleanup_plan(getattr(entry, "after_plan", None))

    def _cleanup_history_stack(self, stack: list[Any]) -> None:
        for entry in stack:
            self._cleanup_history_entry(entry)
        stack.clear()

    def _ensure_stream_workspace(self, estimated_bytes: int, context: str) -> None:
        estimated = max(0, int(estimated_bytes))
        if estimated > self._max_stream_work_bytes:
            raise RuntimeError(
                f"{context} may need about {estimated / (1024**3):.2f} GiB of temporary workspace, "
                f"above streaming.max_temp_workspace_mb={self._max_stream_work_bytes // (1024**2):,}"
            )
        usage = shutil.disk_usage(self._stream_work_dir)
        required = estimated + self._min_free_stream_bytes
        if usage.free < required:
            raise RuntimeError(
                f"{context} needs about {estimated / (1024**2):,.0f} MiB of temporary workspace plus "
                f"{self._min_free_stream_bytes / (1024**2):,.0f} MiB reserve, but only "
                f"{usage.free / (1024**2):,.0f} MiB is free"
            )

    def _save_workspace_estimate(self, block_count: int) -> int:
        # Raw records plus a worst-case compressed payload and modest headers/palette room.
        return int(block_count) * RECORD.size * 2 + 64 * 1024 * 1024

    def _paste_workspace_estimate(self, block_count: int, compressed_bytes: int = 0) -> int:
        # Decoded records + rotated plan. Before/after history streams are only
        # allocated when this operation is inside the configured undo ceiling.
        capture_history = (
            self._history_enabled
            and int(block_count) <= self._history_max_blocks_per_operation
        )
        copies = 4 if capture_history else 2
        return int(compressed_bytes) + int(block_count) * RECORD.size * copies + 64 * 1024 * 1024

    # ------------------------------------------------------------------
    # Main tick loop
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if self._stopping:
            return
        self._tick_counter += 1
        for _ in range(self._completion_budget):
            try:
                callback = self._completion_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:
                self.logger.error(f"Main-thread completion callback failed: {exc}")
                self.logger.debug(traceback.format_exc())

        self._process_save_jobs()
        self._process_paste_jobs()
        if self._tick_counter % self._preview_refresh == 0:
            if self._preview_enabled:
                self._refresh_active_previews()
            if self._selection_preview_enabled:
                self._refresh_selection_previews()

    def _begin_save_region_snapshot(self, job: SaveJob) -> None:
        if job.region_snapshot_active:
            return
        job.region_snapshot_active = True
        job.region_record_start = len(job.records)
        job.region_palette_start = len(job.palette)
        job.region_non_air_start = job.non_air_count
        job.region_block_entity_keys.clear()
        job.region_block_entity_bytes_start = job.block_entity_bytes
        job.region_job_cursor_start = job.cursor

    def _rollback_save_region(self, job: SaveJob) -> None:
        """Discard one chunk-region scan so it can be retried from a known loaded state."""

        truncate = getattr(job.records, "truncate", None)
        if callable(truncate):
            truncate(job.region_record_start)
        else:
            del job.records[job.region_record_start :]
        del job.palette[job.region_palette_start :]
        job.palette_lookup = {
            palette_key(str(entry["type"]), dict(entry.get("states", {}))): index
            for index, entry in enumerate(job.palette)
        }
        job.non_air_count = job.region_non_air_start
        for position in job.region_block_entity_keys:
            job.block_entities.pop(position, None)
        job.region_block_entity_keys.clear()
        job.block_entity_bytes = job.region_block_entity_bytes_start
        job.cursor = job.region_job_cursor_start
        job.region_cursor = 0
        job.region_snapshot_active = False
        job.ready_since_tick = None

    @staticmethod
    def _validate_save_integrity(job: SaveJob) -> tuple[int, int]:
        """Validate deterministic scan accounting before compression/upload."""

        if len(job.records) % RECORD.size:
            raise RuntimeError("saved record buffer is not aligned to the schematic record size")
        if job.cursor != job.total_volume:
            raise RuntimeError(
                f"scan accounting mismatch: visited {job.cursor:,} of {job.total_volume:,} selected blocks"
            )
        if job.verified_regions != len(job.regions):
            raise RuntimeError(
                f"chunk verification mismatch: verified {job.verified_regions:,} of "
                f"{len(job.regions):,} chunk region(s)"
            )
        region_volume = sum(region.volume for region in job.regions)
        if region_volume != job.total_volume:
            raise RuntimeError(
                f"chunk planner mismatch: regions contain {region_volume:,} blocks but selection "
                f"contains {job.total_volume:,}"
            )
        stored = len(job.records) // RECORD.size
        if job.include_air and stored != job.total_volume:
            raise RuntimeError(
                f"full-volume save is incomplete: expected {job.total_volume:,} records, got {stored:,}"
            )
        if stored > job.total_volume:
            raise RuntimeError(
                f"save contains {stored:,} records for a {job.total_volume:,}-block selection"
            )
        return stored, region_volume

    def _process_save_jobs(self) -> None:
        remaining = self._scan_budget
        keys = list(self.save_jobs.keys())
        for index, player_uuid in enumerate(keys):
            if remaining <= 0:
                break
            job = self.save_jobs.get(player_uuid)
            if job is None:
                continue
            active_player = self.server.get_player(player_uuid)
            if active_player is not None and not self.has_schematic_access(active_player):
                self._fail_save_job(job, "schematic access was revoked")
                continue
            region = job.current_region
            if region is None:
                try:
                    self._validate_save_integrity(job)
                except Exception as exc:
                    self._fail_save_job(job, str(exc))
                    continue
                self.save_jobs.pop(player_uuid, None)
                self._finish_save_scan(job)
                continue
            dimension = self._get_dimension(job.dimension_id)
            if dimension is None:
                self._fail_save_job(job, f"dimension '{job.dimension_id}' is unavailable")
                continue
            try:
                if not self._ensure_job_chunk(job, dimension, region.chunk_x, region.chunk_z):
                    continue
                self._begin_save_region_snapshot(job)
                jobs_left = max(1, len(keys) - index)
                share = max(1, remaining // jobs_left)
                count = min(share, job.region_remaining)
                processed = self._scan_save_batch(job, dimension, count)
            except Exception as exc:
                self._fail_save_job(job, str(exc))
                continue
            remaining -= processed

            if processed and not self._job_chunk_is_resident(
                dimension, region.chunk_x, region.chunk_z
            ):
                self._rollback_save_region(job)
                self._release_job_chunk(job, dimension)
                job.chunk_retries += 1
                if job.chunk_retries > self._max_chunk_retries:
                    self._fail_save_job(
                        job,
                        f"chunk {region.chunk_x}, {region.chunk_z} unloaded during scanning "
                        f"after {self._max_chunk_retries:,} retries",
                    )
                    continue
                player = self.server.get_player(player_uuid)
                if player:
                    player.send_message(
                        f"§eChunk {region.chunk_x}, {region.chunk_z} unloaded during scanning; "
                        f"retrying safely ({job.chunk_retries}/{self._max_chunk_retries})."
                    )
                continue

            if job.region_complete:
                job.verified_regions += 1
                job.chunk_retries = 0
                job.region_snapshot_active = False
                self._release_job_chunk(job, dimension)
                job.advance_region()

            if job.cursor >= job.total_volume:
                try:
                    self._validate_save_integrity(job)
                except Exception as exc:
                    self._fail_save_job(job, str(exc))
                    continue
                self.save_jobs.pop(player_uuid, None)
                self._release_job_chunk(job, dimension, release_slot=True)
                self._finish_save_scan(job)
            elif self._tick_counter - job.last_progress_tick >= self._progress_interval:
                job.last_progress_tick = self._tick_counter
                player = self.server.get_player(player_uuid)
                if player:
                    percent = job.cursor * 100.0 / job.total_volume
                    player.send_message(
                        f"§bScanning {job.name}: {percent:.1f}% "
                        f"({job.cursor:,}/{job.total_volume:,}, verified chunk "
                        f"{job.verified_regions + 1}/{len(job.regions)})"
                    )

    def _scan_save_batch(self, job: SaveJob, dimension: Any, count: int) -> int:
        # BlockData's native region call is bounded to 32,768 blocks. Coordinates
        # come from one chunk-contained region, so a 32,000-record slice plus its
        # partial first/last layers always stays below that native ceiling.
        integration = getattr(self, "_blockdata", None)
        if integration is not None:
            count = min(count, 32_000)
        coordinates = list(job.coordinates(count))
        captured_entities: dict[tuple[int, int, int], dict[str, Any]] = {}
        if integration is not None and coordinates:
            xs = [entry[0] for entry in coordinates]
            ys = [entry[1] for entry in coordinates]
            zs = [entry[2] for entry in coordinates]
            captured_entities = integration.capture_region(
                job.dimension_id,
                (min(xs), min(ys), min(zs)),
                (max(xs), max(ys), max(zs)),
            )
        processed = 0
        for x, y, z, dx, dy, dz in coordinates:
            block = dimension.get_block_at(x, y, z)
            data = block.data
            block_type = self.block_data_identifier(data)
            states = dict(data.block_states)
            is_air = block_type in AIR_TYPES
            if not is_air:
                job.non_air_count += 1
            if job.include_air or not is_air:
                key = palette_key(block_type, states)
                palette_index = job.palette_lookup.get(key)
                if palette_index is None:
                    palette_index = len(job.palette)
                    job.palette_lookup[key] = palette_index
                    job.palette.append({"type": block_type, "states": states})
                append_record(job.records, dx, dy, dz, palette_index)
                entity = captured_entities.get((x, y, z))
                if entity is not None:
                    position = (dx, dy, dz)
                    entity_size = self._block_entity_payload_size(entity)
                    if job.block_entity_bytes + entity_size > getattr(
                        self, "_blockdata_max_bytes", 64 * 1024 * 1024
                    ):
                        raise RuntimeError(
                            "captured block-entity data exceeds "
                            f"blockdata.max_uncompressed_mb="
                            f"{getattr(self, '_blockdata_max_bytes', 64 * 1024 * 1024) // (1024**2)}"
                        )
                    job.block_entities[position] = entity
                    job.block_entity_bytes += entity_size
                    job.region_block_entity_keys.add(position)
            job.cursor += 1
            job.region_cursor += 1
            processed += 1
        return processed

    @staticmethod
    def _block_entity_payload_size(entity: dict[str, Any] | None) -> int:
        if entity is None:
            return 0
        return len(
            json.dumps(
                entity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ) + 32

    def _fail_save_job(self, job: SaveJob, reason: str) -> None:
        self.save_jobs.pop(job.player_uuid, None)
        self._release_job_chunk(job, release_slot=True)
        self._close_record_storage(job.records)
        player = self.server.get_player(job.player_uuid)
        if player:
            player.send_error_message(f"Schematic scan failed: {reason}")
        self.logger.error(f"Save scan '{job.name}' failed: {reason}")

    def _finish_save_scan(self, job: SaveJob) -> None:
        stored_count, _region_volume = self._validate_save_integrity(job)
        player = self.server.get_player(job.player_uuid)
        if player:
            player.send_message(
                f"§bVerified {job.verified_regions:,} chunk region(s) and {job.total_volume:,} "
                f"selected blocks. Streaming compression and upload for '{job.name}' off-thread..."
            )

        records = job.records.freeze() if hasattr(job.records, "freeze") else RecordSource(data=bytes(job.records))
        palette = list(job.palette)
        size = job.size
        blockdata = getattr(self, "_blockdata", None)
        header = {
            "name": job.name,
            "display_name": job.display_name,
            "description": job.description,
            "author_uuid": str(job.player_uuid),
            "author_xuid": job.player_xuid,
            "author_name": job.player_name,
            "source_server": self.server_id,
            "source_dimension": job.dimension_id,
            "minecraft_version": str(self.server.minecraft_version),
            "plugin_version": PLUGIN_VERSION,
            "size": list(size),
            "selection_volume": job.total_volume,
            "verified_chunk_regions": job.verified_regions,
            "stored_record_count": stored_count,
            "chunk_residency_verified": True,
            "non_air_count": job.non_air_count,
            "includes_air": job.include_air,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "block_entities": bool(job.block_entities),
            "block_entity_count": len(job.block_entities),
            "blockdata_api_version": (
                blockdata.api_version if blockdata is not None else ""
            ),
            "streaming_codec": True,
        }

        def encode_and_upload() -> dict[str, Any]:
            assert self.store is not None
            payload_path = self._new_payload_path("payload-save-")
            try:
                encoded = encode_schematic_to_file(
                    header,
                    palette,
                    records,
                    payload_path,
                    self._compression_level,
                    block_entities=job.block_entities,
                )
                row = {
                    "namespace": self.store.settings.namespace,
                    "name": job.name,
                    "display_name": job.display_name,
                    "description": job.description,
                    "author_uuid": str(job.player_uuid),
                    "author_xuid": job.player_xuid,
                    "author_name": job.player_name,
                    "source_server": self.server_id,
                    "source_dimension": job.dimension_id,
                    "minecraft_version": str(header["minecraft_version"]),
                    "plugin_version": PLUGIN_VERSION,
                    "format_version": FORMAT_VERSION,
                    "size_x": size[0],
                    "size_y": size[1],
                    "size_z": size[2],
                    "block_count": len(records) // RECORD.size,
                    "non_air_count": job.non_air_count,
                    "palette_count": len(palette),
                    "includes_air": int(job.include_air),
                    "content_sha256": encoded.sha256_hex,
                    "compressed_bytes": encoded.compressed_bytes,
                    "uncompressed_bytes": encoded.uncompressed_bytes,
                    "payload": b"",
                }
                receipt = self.store.save_file(row, payload_path, job.overwrite)
                row.update(receipt)
                return row
            finally:
                records.close()
                payload_path.unlink(missing_ok=True)

        def success(row: dict[str, Any]) -> None:
            current = self.server.get_player(job.player_uuid)
            ratio = 0.0
            if row["uncompressed_bytes"]:
                ratio = 100.0 * (1.0 - row["compressed_bytes"] / row["uncompressed_bytes"])
            storage_note = "inline MySQL payload"
            if row.get("storage") == "chunked":
                storage_note = f"{int(row.get('chunk_count', 0)):,} packet-safe MySQL chunk(s)"
            message = (
                f"§aUploaded and integrity-checked '{job.name}': "
                f"{row['size_x']}×{row['size_y']}×{row['size_z']}, "
                f"{row['block_count']:,} stored records from {job.total_volume:,} scanned blocks across "
                f"{job.verified_regions:,} verified chunk region(s), "
                f"{len(job.block_entities):,} retained block entit{'y' if len(job.block_entities) == 1 else 'ies'}, "
                f"{row['compressed_bytes'] / 1024:.1f} KiB ({ratio:.1f}% compression), "
                f"stored as {storage_note} with bounded-memory streaming."
            )
            if current:
                current.send_message(message)
            self.logger.info(message.replace("§a", ""))

        def failure(error: BaseException) -> None:
            records.close()
            current = self.server.get_player(job.player_uuid)
            if current:
                current.send_error_message(f"Upload failed: {error}")
            self.logger.error(f"Upload '{job.name}' failed: {error}")

        self._submit_worker(encode_and_upload, success, failure)

    def _process_paste_jobs(self) -> None:
        remaining = self._paste_budget
        tick_deadline = monotonic() + getattr(self, "_paste_time_budget_seconds", 0.010)
        keys = list(self.paste_jobs.keys())
        for index, player_uuid in enumerate(keys):
            if remaining <= 0 or monotonic() >= tick_deadline:
                break
            job = self.paste_jobs.get(player_uuid)
            if job is None:
                continue
            active_player = self.server.get_player(player_uuid)
            if active_player is not None and not self.has_schematic_access(active_player):
                self._fail_paste_job(job, "schematic access was revoked")
                continue
            chunk = job.current_chunk
            if chunk is None:
                integrity_error = self._paste_integrity_error(job)
                if integrity_error:
                    self._fail_paste_job(job, integrity_error)
                else:
                    self._complete_paste_job(job)
                continue
            dimension = self._get_dimension(job.dimension_id)
            if dimension is None:
                self._fail_paste_job(job, f"dimension '{job.dimension_id}' is unavailable")
                continue
            try:
                if not self._ensure_job_chunk(job, dimension, chunk.chunk_x, chunk.chunk_z):
                    continue
                now = monotonic()
                if now >= tick_deadline:
                    break
                jobs_left = max(1, len(keys) - index)
                share = max(1, remaining // jobs_left)
                count = min(share, job.chunk_remaining)
                # A record count alone cannot predict main-thread cost: block states,
                # custom blocks, chunk generation, and write verification can make one
                # region much more expensive than another. Give every active job a fair
                # share of a real-time tick deadline as well as the configured count cap.
                job_deadline = now + max(0.001, (tick_deadline - now) / jobs_left)
                processed = self._paste_batch(
                    job,
                    dimension,
                    count,
                    deadline=min(tick_deadline, job_deadline),
                )
            except Exception as exc:
                self._fail_paste_job(job, str(exc))
                continue
            remaining -= processed
            if processed and not self._job_chunk_is_resident(
                dimension, chunk.chunk_x, chunk.chunk_z
            ):
                self._fail_paste_job(
                    job,
                    f"chunk {chunk.chunk_x}, {chunk.chunk_z} unloaded during {job.operation}; "
                    "the operation was stopped instead of reporting a partial success",
                )
                continue
            if job.chunk_complete:
                self._release_job_chunk(job, dimension)
                job.advance_chunk()
            if job.cursor >= job.plan.block_count:
                integrity_error = self._paste_integrity_error(job)
                if integrity_error:
                    self._fail_paste_job(job, integrity_error)
                else:
                    self._complete_paste_job(job)
            elif self._tick_counter - job.last_progress_tick >= self._progress_interval:
                job.last_progress_tick = self._tick_counter
                player = self.server.get_player(player_uuid)
                if player:
                    percent = job.cursor * 100.0 / max(1, job.plan.block_count)
                    player.send_message(
                        f"§dPasting {job.name}: {percent:.1f}% "
                        f"({job.cursor:,}/{job.plan.block_count:,}, "
                        f"chunk {job.chunk_index + 1}/{len(job.plan.chunks)})"
                    )

    def _create_type_only_block_data(self, block_type: str) -> Any:
        """Create block data without source states across Endstone 0.11 variants."""

        try:
            return self.server.create_block_data(block_type)
        except TypeError:
            return self.server.create_block_data(block_type, {})

    def _resolve_palette_entry(
        self,
        job: PasteJob,
        palette_index: int,
        block_type: str,
        states: dict[str, Any],
    ) -> tuple[str, Any | None]:
        """Resolve a palette entry once and cache unavailable/type-only results."""

        cached = job.palette_modes.get(palette_index)
        if cached == "missing":
            return cached, None
        if cached == "type_only":
            return cached, self._create_type_only_block_data(block_type)
        try:
            data = self.server.create_block_data(block_type, states)
        except Exception:
            try:
                data = self._create_type_only_block_data(block_type)
            except Exception:
                job.palette_modes[palette_index] = "missing"
                return "missing", None
            job.palette_modes[palette_index] = "type_only"
            return "type_only", data
        job.palette_modes[palette_index] = "exact"
        return "exact", data

    def _record_missing_block(self, job: PasteJob, block_type: str) -> None:
        job.missing_blocks += 1
        job.missing_type_counts[block_type] = job.missing_type_counts.get(block_type, 0) + 1

    def _missing_block_note(self, job: PasteJob) -> str:
        if not job.missing_blocks:
            return ""
        ordered = sorted(
            job.missing_type_counts.items(), key=lambda item: (-item[1], item[0])
        )
        visible = ordered[: self._missing_block_report_limit]
        names = ", ".join(f"{name} ×{count:,}" for name, count in visible)
        extra = len(ordered) - len(visible)
        if extra > 0:
            names += f", plus {extra:,} more type(s)"
        if self._missing_block_policy == "skip":
            action = "skipped"
        elif self._missing_block_policy == "abort":
            action = "encountered before the operation stopped"
        else:
            action = "substituted"
        return (
            f" {job.missing_blocks:,} unavailable custom-block record(s) were {action} "
            f"under policy '{self._missing_block_policy}': {names}."
        )

    def _log_missing_blocks(self, job: PasteJob) -> None:
        if not job.missing_blocks:
            return
        ordered = sorted(
            job.missing_type_counts.items(), key=lambda item: (-item[1], item[0])
        )
        details = ", ".join(f"{name}={count:,}" for name, count in ordered)
        self.logger.warning(
            f"{job.operation.title()} '{job.name}' encountered {job.missing_blocks:,} unavailable "
            f"block record(s) under policy '{self._missing_block_policy}': {details}"
        )

    def _paste_batch(
        self,
        job: PasteJob,
        dimension: Any,
        count: int,
        *,
        deadline: float | None = None,
    ) -> int:
        end = min(job.plan.block_count, job.cursor + count)
        processed = 0
        max_failures = getattr(self, "_max_paste_failures", 0)
        verify_writes = getattr(self, "_verify_paste_writes", True)
        integration = getattr(self, "_blockdata", None)
        strict_blockdata = getattr(self, "_blockdata_strict_restore", True)

        for record_index in range(job.cursor, end):
            # Always allow one record to make forward progress, then yield as soon as
            # this job's wall-clock share is spent. A single slow native call cannot be
            # pre-empted, but it can no longer be followed by another 1,199 calls in the
            # same server tick.
            if processed and deadline is not None and monotonic() >= deadline:
                break
            dx, dy, dz, palette_index = record_at(job.plan.records, record_index)
            failure_reason = ""
            if palette_index >= len(job.plan.palette):
                failure_reason = f"record {record_index:,} references palette index {palette_index}"
                job.failed += 1
                job.cursor += 1
                processed += 1
                if job.failed > max_failures:
                    raise RuntimeError(
                        f"paste verification stopped after {job.failed:,} failure(s): {failure_reason}"
                    )
                continue

            target = dimension.get_block_at(
                job.anchor.x + dx, job.anchor.y + dy, job.anchor.z + dz
            )
            entry = job.plan.palette[palette_index]
            requested_type = str(entry["type"])
            requested_states = dict(entry.get("states", {}))
            current_type = ""
            current_states: dict[str, Any] = {}
            after_type = ""
            after_states: dict[str, Any] = {}
            relative_position = (dx, dy, dz)
            world_position = (
                job.anchor.x + dx,
                job.anchor.y + dy,
                job.anchor.z + dz,
            )
            desired_entity = job.plan.block_entities.get(relative_position)
            before_entity: dict[str, Any] | None = None
            after_entity: dict[str, Any] | None = None
            changed = False

            try:
                current = target.data
                current_type = self.block_data_identifier(current)
                current_states = dict(current.block_states)
                if integration is not None and (desired_entity is not None or job.capture_history):
                    before_entity = integration.capture(job.dimension_id, world_position)
                elif desired_entity is not None and strict_blockdata:
                    detail = getattr(self, "_blockdata_error", "BlockData API is unavailable")
                    raise BlockDataIntegrationError(
                        f"retained block data cannot be restored because {detail}"
                    )

                mode, block_data = self._resolve_palette_entry(
                    job, palette_index, requested_type, requested_states
                )
                desired_type = requested_type
                desired_states = requested_states
                require_exact_states = mode == "exact"
                state_fallback = mode == "type_only"
                missing_substitution = False

                if mode == "missing":
                    self._record_missing_block(job, requested_type)
                    if self._missing_block_policy == "abort":
                        raise RuntimeError(
                            f"block type {requested_type} is unavailable in the target server registry"
                        )
                    if self._missing_block_policy == "skip":
                        job.skipped += 1
                        job.cursor += 1
                        processed += 1
                        continue

                    desired_type = (
                        "minecraft:air"
                        if self._missing_block_policy == "air"
                        else self._missing_block_fallback
                    )
                    desired_states = {}
                    require_exact_states = False
                    missing_substitution = True
                    job.missing_substitutions += 1
                    try:
                        block_data = self._create_type_only_block_data(desired_type)
                    except Exception as exc:
                        raise RuntimeError(
                            f"missing-block fallback {desired_type} is not available: {exc}"
                        ) from exc

                base_matches = current_type == desired_type and (
                    not require_exact_states or current_states == desired_states
                )
                entity_matches = desired_entity is None or before_entity == desired_entity
                if self._skip_unchanged and base_matches and entity_matches:
                    job.skipped += 1
                    job.cursor += 1
                    processed += 1
                    continue

                try:
                    target.set_data(block_data, apply_physics=self._apply_physics)
                except Exception:
                    # State payloads differ between Bedrock versions and custom packs. If
                    # the type exists, fall back to its default state instead of failing.
                    target.set_type(desired_type, apply_physics=self._apply_physics)
                    require_exact_states = False
                    if not missing_substitution:
                        state_fallback = True
                        job.palette_modes[palette_index] = "type_only"

                if state_fallback:
                    job.state_fallbacks += 1

                after = target.data
                after_type = self.block_data_identifier(after)
                after_states = dict(after.block_states)
                changed = current_type != after_type or current_states != after_states
                verified = after_type == desired_type and (
                    not require_exact_states or after_states == desired_states
                )

                if verify_writes and not verified and require_exact_states:
                    # One immediate retry handles a transient block update without advancing the cursor.
                    target.set_data(
                        self.server.create_block_data(desired_type, desired_states),
                        apply_physics=self._apply_physics,
                    )
                    after = target.data
                    after_type = self.block_data_identifier(after)
                    after_states = dict(after.block_states)
                    changed = current_type != after_type or current_states != after_states
                    verified = after_type == desired_type and after_states == desired_states

                blockdata_error = ""
                if verified and desired_entity is not None:
                    if integration is None:
                        blockdata_error = (
                            "retained block data was not restored because the BlockData API "
                            "is unavailable"
                        )
                    else:
                        try:
                            integration.restore(job.dimension_id, world_position, desired_entity)
                            job.block_entities_restored += 1
                        except Exception as exc:
                            blockdata_error = str(exc)
                    if blockdata_error:
                        job.block_entity_failures += 1

                if integration is not None and job.capture_history:
                    try:
                        after_entity = integration.capture(job.dimension_id, world_position)
                    except Exception as exc:
                        if strict_blockdata:
                            blockdata_error = blockdata_error or (
                                f"BlockData history capture failed: {exc}"
                            )
                            job.block_entity_failures += 1
                changed = changed or before_entity != after_entity

                if verify_writes and not verified:
                    failure_reason = (
                        f"write verification failed at "
                        f"{job.anchor.x + dx}, {job.anchor.y + dy}, {job.anchor.z + dz}: "
                        f"expected {desired_type} {desired_states}, got {after_type} {after_states}"
                    )
                    job.failed += 1
                elif blockdata_error and strict_blockdata:
                    failure_reason = (
                        f"block-data restore failed at "
                        f"{world_position[0]}, {world_position[1]}, {world_position[2]}: "
                        f"{blockdata_error}"
                    )
                    job.failed += 1
                else:
                    job.placed += 1
                    if blockdata_error:
                        self.logger.warning(
                            f"{job.operation.title()} '{job.name}' placed the base block at "
                            f"{world_position} without retained BlockData metadata: {blockdata_error}"
                        )

                if job.capture_history and changed:
                    self._capture_history_change(
                        job,
                        dx,
                        dy,
                        dz,
                        current_type,
                        current_states,
                        after_type,
                        after_states,
                        before_entity,
                        after_entity,
                    )
            except Exception as exc:
                if not failure_reason:
                    failure_reason = (
                        f"block operation failed at "
                        f"{job.anchor.x + dx}, {job.anchor.y + dy}, {job.anchor.z + dz}: {exc}"
                    )
                    job.failed += 1

            job.cursor += 1
            processed += 1
            if job.failed > max_failures:
                raise RuntimeError(
                    f"paste verification stopped after {job.failed:,} failure(s): {failure_reason}"
                )
        return processed

    @staticmethod
    def _paste_integrity_error(job: PasteJob) -> str | None:
        expected = job.plan.block_count
        accounted = job.placed + job.skipped + job.failed
        if job.cursor != expected:
            return f"processed {job.cursor:,} of {expected:,} planned records"
        if accounted != expected:
            return (
                f"result accounting mismatch: placed={job.placed:,}, skipped={job.skipped:,}, "
                f"failed={job.failed:,}, expected={expected:,}"
            )
        return None

    def _capture_history_change(
        self,
        job: PasteJob,
        dx: int,
        dy: int,
        dz: int,
        before_type: str,
        before_states: dict[str, Any],
        after_type: str,
        after_states: dict[str, Any],
        before_entity: dict[str, Any] | None = None,
        after_entity: dict[str, Any] | None = None,
    ) -> None:
        if not job.capture_history:
            return
        if job.captured_blocks >= self._history_max_blocks_per_operation:
            self._disable_history_capture(
                job,
                f"changed-block count exceeded the configured history limit "
                f"({self._history_max_blocks_per_operation:,})",
            )
            return

        before_entity_size = self._block_entity_payload_size(before_entity)
        after_entity_size = self._block_entity_payload_size(after_entity)
        entity_limit = getattr(self, "_blockdata_max_bytes", 64 * 1024 * 1024)
        if (
            job.before_block_entity_bytes + before_entity_size > entity_limit
            or job.after_block_entity_bytes + after_entity_size > entity_limit
        ):
            self._disable_history_capture(
                job,
                "retained BlockData history exceeded "
                f"blockdata.max_uncompressed_mb={entity_limit // (1024**2)}",
            )
            return

        chunk = ((job.anchor.x + dx) // 16, (job.anchor.z + dz) // 16)
        record_count = job.captured_blocks
        if job.history_chunk != chunk:
            if job.history_chunk is not None:
                job.history_chunks.append(
                    PasteChunkRange(
                        chunk_x=job.history_chunk[0],
                        chunk_z=job.history_chunk[1],
                        start=job.history_chunk_start,
                        end=record_count,
                    )
                )
            job.history_chunk = chunk
            job.history_chunk_start = record_count

        before_index = self._history_palette_index(
            job.before_palette_lookup,
            job.before_palette,
            before_type,
            before_states,
        )
        after_index = self._history_palette_index(
            job.after_palette_lookup,
            job.after_palette,
            after_type,
            after_states,
        )
        append_record(job.before_records, dx, dy, dz, before_index)
        append_record(job.after_records, dx, dy, dz, after_index)
        position = (dx, dy, dz)
        if before_entity is not None:
            job.before_block_entities[position] = before_entity
            job.before_block_entity_bytes += before_entity_size
        if after_entity is not None:
            job.after_block_entities[position] = after_entity
            job.after_block_entity_bytes += after_entity_size

    def _disable_history_capture(self, job: PasteJob, reason: str) -> None:
        job.capture_history = False
        job.history_disabled_reason = reason
        job.before_palette_lookup.clear()
        job.before_palette.clear()
        self._close_record_storage(job.before_records)
        job.before_records = bytearray()
        job.after_palette_lookup.clear()
        job.after_palette.clear()
        self._close_record_storage(job.after_records)
        job.after_records = bytearray()
        job.before_block_entities.clear()
        job.after_block_entities.clear()
        job.before_block_entity_bytes = 0
        job.after_block_entity_bytes = 0
        job.history_chunks.clear()
        job.history_chunk = None

    @staticmethod
    def _history_palette_index(
        lookup: dict[Any, int],
        palette: list[dict[str, Any]],
        block_type: str,
        states: dict[str, Any],
    ) -> int:
        key = (block_type, tuple(sorted(states.items())))
        index = lookup.get(key)
        if index is None:
            index = len(palette)
            lookup[key] = index
            palette.append({"type": block_type, "states": dict(states)})
        return index

    def _history_entry_from_job(self, job: PasteJob) -> HistoryEntry | None:
        if not job.capture_history or job.captured_blocks <= 0:
            return None
        count = job.captured_blocks
        chunks = list(job.history_chunks)
        if job.history_chunk is not None:
            chunks.append(
                PasteChunkRange(
                    chunk_x=job.history_chunk[0],
                    chunk_z=job.history_chunk[1],
                    start=job.history_chunk_start,
                    end=count,
                )
            )
        before_records = (
            job.before_records.freeze()
            if hasattr(job.before_records, "freeze")
            else RecordSource(data=bytes(job.before_records))
        )
        after_records = (
            job.after_records.freeze()
            if hasattr(job.after_records, "freeze")
            else RecordSource(data=bytes(job.after_records))
        )
        # Transfer ownership to the history entry so the job cannot freeze them twice.
        job.before_records = bytearray()
        job.after_records = bytearray()
        job.capture_history = False
        before_plan = PastePlan(
            size=job.plan.size,
            palette=list(job.before_palette),
            records=before_records,
            chunks=tuple(chunks),
            block_entities=dict(job.before_block_entities),
        )
        after_plan = PastePlan(
            size=job.plan.size,
            palette=list(job.after_palette),
            records=after_records,
            chunks=tuple(chunks),
            block_entities=dict(job.after_block_entities),
        )
        return HistoryEntry(
            name=job.name,
            dimension_id=job.dimension_id,
            anchor=job.anchor,
            before_plan=before_plan,
            after_plan=after_plan,
            block_count=count,
            created_tick=self._tick_counter,
        )

    def _push_undo_history(self, player_uuid: Any, entry: HistoryEntry, *, clear_redo: bool) -> None:
        stack = self.undo_history.setdefault(player_uuid, [])
        stack.append(entry)
        if clear_redo:
            old_redo = self.redo_history.pop(player_uuid, [])
            self._cleanup_history_stack(old_redo)
        while len(stack) > self._history_max_operations:
            self._cleanup_history_entry(stack.pop(0))
        while sum(item.block_count for item in stack) > self._history_max_total_blocks and len(stack) > 1:
            self._cleanup_history_entry(stack.pop(0))

    def _complete_paste_job(self, job: PasteJob) -> None:
        integrity_error = self._paste_integrity_error(job)
        if integrity_error:
            self._fail_paste_job(job, integrity_error)
            return
        self.paste_jobs.pop(job.player_uuid, None)
        self._release_job_chunk(job, release_slot=True)
        player = self.server.get_player(job.player_uuid)

        if job.operation == "paste":
            history_entry = self._history_entry_from_job(job)
            if history_entry is not None:
                self._push_undo_history(job.player_uuid, history_entry, clear_redo=True)
            missing_note = self._missing_block_note(job)
            self._log_missing_blocks(job)
            if player:
                blockdata_note = ""
                if job.block_entities_restored or job.block_entity_failures:
                    blockdata_note = (
                        f" BlockData: {job.block_entities_restored:,} restored, "
                        f"{job.block_entity_failures:,} failed."
                    )
                history_note = ""
                if history_entry is not None:
                    history_note = f" Undo captured for {history_entry.block_count:,} changed blocks."
                elif job.history_disabled_reason:
                    history_note = f" Undo was not captured because {job.history_disabled_reason}."
                player.send_message(
                    f"§aPaste complete: {job.placed:,} blocks placed, {job.skipped:,} unchanged or unavailable skipped, "
                    f"{job.state_fallbacks:,} state fallbacks, {job.failed:,} failures."
                    f"{blockdata_note}{missing_note}{history_note}"
                )
            self._cleanup_plan(job.plan)
            self._close_record_storage(job.before_records)
            self._close_record_storage(job.after_records)
            return

        entry = job.history_entry
        if entry is not None:
            if job.operation == "undo":
                source = self.undo_history.setdefault(job.player_uuid, [])
                if source and source[-1] is entry:
                    source.pop()
                self.redo_history.setdefault(job.player_uuid, []).append(entry)
            elif job.operation == "redo":
                source = self.redo_history.setdefault(job.player_uuid, [])
                if source and source[-1] is entry:
                    source.pop()
                self._push_undo_history(job.player_uuid, entry, clear_redo=False)
        missing_note = self._missing_block_note(job)
        self._log_missing_blocks(job)
        if player:
            verb = "Undo" if job.operation == "undo" else "Redo"
            blockdata_note = ""
            if job.block_entities_restored or job.block_entity_failures:
                blockdata_note = (
                    f" BlockData: {job.block_entities_restored:,} restored, "
                    f"{job.block_entity_failures:,} failed."
                )
            player.send_message(
                f"§a{verb} complete for '{job.name}': {job.placed:,} blocks restored, "
                f"{job.skipped:,} already matched or unavailable, {job.failed:,} failures."
                f"{blockdata_note}{missing_note}"
            )

    def _fail_paste_job(self, job: PasteJob, reason: str) -> None:
        self.paste_jobs.pop(job.player_uuid, None)
        self._release_job_chunk(job, release_slot=True)
        partial = None
        if job.operation == "paste":
            partial = self._history_entry_from_job(job)
            if partial is not None:
                self._push_undo_history(job.player_uuid, partial, clear_redo=True)
            self._cleanup_plan(job.plan)
            self._close_record_storage(job.before_records)
            self._close_record_storage(job.after_records)
        player = self.server.get_player(job.player_uuid)
        missing_note = self._missing_block_note(job)
        self._log_missing_blocks(job)
        if player:
            suffix = ""
            if partial is not None:
                suffix = f" A partial undo was saved for {partial.block_count:,} changed blocks."
            player.send_error_message(
                f"Schematic {job.operation} failed: {reason}.{missing_note}{suffix}"
            )
        self.logger.error(f"{job.operation.title()} '{job.name}' failed: {reason}")

    def _dispatch_console_command(self, command_line: str) -> bool:
        """Dispatch one Bedrock command from the server console."""

        dispatcher = getattr(self.server, "dispatch_command", None)
        sender = getattr(self.server, "command_sender", None)
        if not callable(dispatcher) or sender is None:
            return False
        try:
            return bool(dispatcher(sender, command_line))
        except Exception as exc:
            self.logger.debug(f"Console command failed '{command_line}': {exc}")
            return False

    @property
    def _legacy_ticket_registry_path(self) -> Path:
        return Path(self.data_folder) / "legacy_tickingareas.json"

    def _load_legacy_ticket_registry(self) -> list[dict[str, Any]]:
        """Read ticking areas that may have survived an unclean server shutdown."""

        path = self._legacy_ticket_registry_path
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning(f"Unable to read stale ticking-area journal: {exc}")
            return []
        records = payload.get("tickets", []) if isinstance(payload, dict) else []
        valid: list[dict[str, Any]] = []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            name = str(record.get("name", "")).strip()
            dimension_id = str(record.get("dimension_id", "")).strip()
            if not name or not dimension_id:
                continue
            try:
                chunk_x = int(record.get("chunk_x", 0))
                chunk_z = int(record.get("chunk_z", 0))
            except (TypeError, ValueError):
                continue
            valid.append(
                {
                    "name": name,
                    "dimension_id": dimension_id,
                    "chunk_x": chunk_x,
                    "chunk_z": chunk_z,
                }
            )
        return valid

    def _write_legacy_ticket_registry(
        self, records: dict[str, dict[str, Any]] | None = None
    ) -> None:
        """Atomically persist currently owned legacy ticking-area names."""

        if not getattr(self, "data_folder", None):
            return
        path = self._legacy_ticket_registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        source = self._legacy_ticket_registry if records is None else records
        payload = {
            "version": 1,
            "tickets": list(source.values()),
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _remember_legacy_ticket(
        self,
        name: str,
        dimension_id: Any,
        chunk_x: int,
        chunk_z: int,
    ) -> None:
        """Journal one legacy ticket once per active job/session slot."""

        record = {
            "name": str(name),
            "dimension_id": str(dimension_id),
            "chunk_x": int(chunk_x),
            "chunk_z": int(chunk_z),
        }
        registry = getattr(self, "_legacy_ticket_registry", None)
        if registry is None:
            registry = {}
            self._legacy_ticket_registry = registry
        if registry.get(str(name)) == record:
            return
        registry[str(name)] = record
        self._write_legacy_ticket_registry()

    def _forget_legacy_ticket(self, name: str | None) -> None:
        registry = getattr(self, "_legacy_ticket_registry", None)
        if not name or registry is None or registry.pop(str(name), None) is None:
            return
        self._write_legacy_ticket_registry()

    def _cleanup_stale_legacy_tickets(self) -> None:
        """Remove only journaled ticking areas left by an unclean shutdown."""

        if not getattr(self, "_legacy_tickingarea_fallback", False):
            return
        records = list(getattr(self, "_stale_legacy_ticket_records", []))
        self._stale_legacy_ticket_records = []
        if not records:
            return
        removed = 0
        for record in records:
            try:
                if self._dispatch_console_command(
                    tickingarea_remove_command(record["dimension_id"], record["name"])
                ):
                    removed += 1
            except (KeyError, ValueError):
                continue
        self.logger.info(
            f"Legacy ticking-area recovery checked {len(records):,} journaled area(s); "
            f"{removed:,} removal command(s) succeeded."
        )

    def _allocate_legacy_ticket_slot(self, job: Any) -> int | None:
        current = getattr(job, "ticket_slot", None)
        if current is not None and self._legacy_ticket_slots.get(current) is job:
            return current
        for slot in range(self._legacy_tickingarea_max_active):
            if slot not in self._legacy_ticket_slots:
                self._legacy_ticket_slots[slot] = job
                job.ticket_slot = slot
                return slot
        return None

    def _request_job_chunk_ticket(
        self, job: Any, dimension: Any, chunk_x: int, chunk_z: int
    ) -> bool:
        """Acquire a direct Endstone ticket or a temporary Bedrock ticking area."""

        load_chunk = getattr(dimension, "load_chunk", None)
        if callable(load_chunk):
            if not bool(load_chunk(chunk_x, chunk_z)):
                raise RuntimeError(f"Endstone refused a load ticket for chunk {chunk_x}, {chunk_z}")
            job.ticket_chunk = (chunk_x, chunk_z)
            job.ticket_owned = True
            job.ticket_backend = "endstone"
            job.waiting_since_tick = self._tick_counter
            job.ready_since_tick = None
            return True

        if not self._legacy_tickingarea_fallback:
            raise RuntimeError(
                f"chunk {chunk_x}, {chunk_z} needs a load ticket, but this Endstone build "
                "does not expose load_chunk and legacy_tickingarea_fallback is disabled"
            )
        slot = self._allocate_legacy_ticket_slot(job)
        if slot is None:
            if job.waiting_since_tick is None:
                job.waiting_since_tick = self._tick_counter
            if self._tick_counter - job.waiting_since_tick > self._chunk_load_timeout:
                raise RuntimeError(
                    "timed out waiting for a temporary ticking-area slot; reduce concurrent "
                    "schematic jobs or increase legacy_tickingarea_max_active"
                )
            return False

        name = getattr(job, "ticket_name", None)
        if not name:
            session = getattr(self, "_legacy_ticket_session", "boot")
            session_prefix = f"{self._legacy_tickingarea_prefix}_{session}"
            name = build_ticket_name(session_prefix, slot)
            job.ticket_name = name
            self._remember_legacy_ticket(name, job.dimension_id, chunk_x, chunk_z)
        command = tickingarea_add_command(
            job.dimension_id,
            chunk_x,
            chunk_z,
            name,
            preload=self._legacy_tickingarea_preload,
        )
        if not self._dispatch_console_command(command):
            self._legacy_ticket_slots.pop(slot, None)
            job.ticket_slot = None
            self._forget_legacy_ticket(name)
            job.ticket_name = None
            raise RuntimeError(
                f"unable to create temporary ticking area for chunk {chunk_x}, {chunk_z}; "
                "confirm cheats are enabled and the console may run /tickingarea"
            )
        job.ticket_chunk = (chunk_x, chunk_z)
        job.ticket_owned = True
        job.ticket_backend = "tickingarea"
        job.waiting_since_tick = self._tick_counter
        job.ready_since_tick = None
        return True

    def _ensure_job_chunk(self, job: Any, dimension: Any, chunk_x: int, chunk_z: int) -> bool:
        """Acquire, verify, and stabilize a held chunk before touching world blocks."""

        requested = (int(chunk_x), int(chunk_z))
        current_ticket = getattr(job, "ticket_chunk", None)
        needs_ticket = self._auto_load_chunks and not getattr(job, "ticket_owned", False)
        if current_ticket != requested or needs_ticket:
            if (
                current_ticket is not None
                or getattr(job, "ticket_owned", False)
                or getattr(job, "ticket_slot", None) is not None
            ):
                self._release_job_chunk(job, dimension)
            if self._auto_load_chunks:
                if not self._request_job_chunk_ticket(job, dimension, *requested):
                    return False
                # Bedrock chunk loads are asynchronous, even when a ticket is accepted.
                return False
            job.ticket_chunk = requested
            job.ticket_backend = "observed"
            job.waiting_since_tick = self._tick_counter

        state = chunk_loaded_state(dimension, *requested)
        if state is False:
            job.ready_since_tick = None
            if not self._auto_load_chunks:
                raise RuntimeError(
                    f"chunk {chunk_x}, {chunk_z} is not loaded; move near it or enable "
                    "auto_load_missing_chunks"
                )
            if job.waiting_since_tick is None:
                job.waiting_since_tick = self._tick_counter
            if self._tick_counter - job.waiting_since_tick > self._chunk_load_timeout:
                raise RuntimeError(f"timed out waiting for chunk {chunk_x}, {chunk_z} to load")
            return False

        if state is None and not self._auto_load_chunks:
            raise RuntimeError(
                f"this Endstone build cannot prove chunk {chunk_x}, {chunk_z} is loaded; "
                "enable auto_load_missing_chunks so the plugin can hold it"
            )

        # If the old runtime cannot expose loaded state, a held preloaded ticking area is
        # accepted only after the same stabilization delay used for positively observed loads.
        if job.ready_since_tick is None:
            job.ready_since_tick = self._tick_counter
            return self._chunk_stabilize_ticks <= 0
        if self._tick_counter - job.ready_since_tick < self._chunk_stabilize_ticks:
            return False
        return True

    @staticmethod
    def _job_chunk_is_resident(dimension: Any, chunk_x: int, chunk_z: int) -> bool:
        """Return false only when the runtime positively reports that a chunk dropped."""

        state = chunk_loaded_state(dimension, chunk_x, chunk_z)
        return state is not False

    def _release_job_chunk(
        self,
        job: Any,
        dimension: Any | None = None,
        *,
        release_slot: bool = False,
    ) -> None:
        ticket = getattr(job, "ticket_chunk", None)
        backend = getattr(job, "ticket_backend", None)
        owned = bool(getattr(job, "ticket_owned", False))
        legacy_remove_succeeded = True
        if owned and ticket:
            if dimension is None:
                dimension = self._get_dimension(job.dimension_id)
            if backend == "endstone" and dimension is not None:
                # unload_chunk() forces pending unloads and chunk saves to complete
                # synchronously. Endstone documents that calling it once per chunk over
                # a large area is expensive, so prefer the deferred release API.
                release_chunk = getattr(dimension, "unload_chunk_request", None)
                if not callable(release_chunk):
                    release_chunk = getattr(dimension, "unload_chunk", None)
                if callable(release_chunk):
                    try:
                        release_chunk(ticket[0], ticket[1])
                    except Exception as exc:
                        self.logger.debug(f"Unable to release Endstone chunk ticket {ticket}: {exc}")
            elif backend == "tickingarea":
                name = getattr(job, "ticket_name", None)
                if name:
                    try:
                        legacy_remove_succeeded = self._dispatch_console_command(
                            tickingarea_remove_command(job.dimension_id, name)
                        )
                    except ValueError as exc:
                        legacy_remove_succeeded = False
                        self.logger.debug(f"Unable to remove ticking area {name}: {exc}")

        slot = getattr(job, "ticket_slot", None)
        if release_slot:
            if slot is not None and self._legacy_ticket_slots.get(slot) is job:
                self._legacy_ticket_slots.pop(slot, None)
            if backend != "tickingarea" or not owned or legacy_remove_succeeded:
                self._forget_legacy_ticket(getattr(job, "ticket_name", None))
        job.ticket_chunk = None
        job.ticket_owned = False
        job.ticket_backend = None
        if release_slot:
            job.ticket_name = None
            job.ticket_slot = None
        job.waiting_since_tick = None
        job.ready_since_tick = None

    # ------------------------------------------------------------------
    # Selection and save operations
    # ------------------------------------------------------------------

    def set_selection_from_block(self, player: Player, which: int, block: Any) -> None:
        if not self.require_schematic_access(player):
            return
        selection = self.selections.setdefault(player.unique_id, Selection())
        dimension_id = self.dimension_identifier(block.dimension)
        position = BlockPos(int(block.x), int(block.y), int(block.z))
        selection.set_position(which, dimension_id, position)
        player.send_message(
            f"§bPosition {which} set to {position.x}, {position.y}, {position.z} in {dimension_id}."
        )
        if selection.complete:
            sx, sy, sz = selection.size
            player.send_message(f"§7Selection: {sx}×{sy}×{sz} = {selection.volume:,} blocks.")
            self._spawn_selection_preview(player, selection)

    def set_selection_at_player(self, player: Player, which: int, coordinates: list[int] | None = None) -> None:
        if not self.require_schematic_access(player):
            return
        location = player.location
        if coordinates is None:
            position = BlockPos(location.block_x, location.block_y, location.block_z)
        else:
            position = BlockPos(*coordinates)
        selection = self.selections.setdefault(player.unique_id, Selection())
        selection.set_position(which, self.dimension_identifier(location.dimension), position)
        player.send_message(f"§bPosition {which} set to {position.x}, {position.y}, {position.z}.")
        if selection.complete:
            sx, sy, sz = selection.size
            player.send_message(f"§7Selection: {sx}×{sy}×{sz} = {selection.volume:,} blocks.")
            self._spawn_selection_preview(player, selection)

    def start_save(
        self,
        player: Player,
        name: str,
        description: str = "",
        include_air: bool | None = None,
        overwrite: bool | None = None,
    ) -> None:
        if not self.require_schematic_access(player):
            return
        if not self._require_database(player):
            return
        if (
            player.unique_id in self.save_jobs
            or player.unique_id in self.paste_jobs
            or player.unique_id in self.preparing_pastes
        ):
            player.send_error_message("You already have an active schematic job. Use /schem cancel first.")
            return
        selection = self.selections.get(player.unique_id)
        if not selection or not selection.complete or selection.dimension_id is None:
            player.send_error_message("Select both corners first.")
            return
        try:
            normalized = normalize_schematic_name(name)
        except ValueError as exc:
            player.send_error_message(str(exc))
            return
        if selection.volume > self._max_blocks:
            player.send_error_message(
                f"Selection has {selection.volume:,} blocks; the configured maximum is {self._max_blocks:,}."
            )
            return
        try:
            self._ensure_stream_workspace(
                self._save_workspace_estimate(selection.volume),
                f"saving a {selection.volume:,}-block schematic",
            )
        except RuntimeError as exc:
            player.send_error_message(str(exc))
            return
        schem_config = self.config.get("schematics", {})
        if include_air is None:
            include_air = bool(schem_config.get("include_air_default", True))
        if overwrite is None:
            overwrite = bool(schem_config.get("allow_overwrite_default", False))
        low, _high = selection.bounds()
        display_name = name.strip()[:128] or normalized
        job = SaveJob(
            player_uuid=player.unique_id,
            player_name=str(player.name)[:64],
            player_xuid=str(getattr(player, "xuid", ""))[:32],
            name=normalized,
            display_name=display_name,
            description=description.strip()[:4096],
            overwrite=bool(overwrite),
            include_air=bool(include_air),
            dimension_id=selection.dimension_id,
            low=low,
            size=selection.size,
            total_volume=selection.volume,
            regions=build_chunk_regions(low, selection.size),
            records=(self._new_record_buffer("records-save-") if self._streaming_enabled else bytearray()),
            started_tick=self._tick_counter,
            last_progress_tick=self._tick_counter,
        )
        self.save_jobs[player.unique_id] = job
        player.send_message(
            f"§aStarted scanning '{normalized}' across ticks: {selection.volume:,} blocks, "
            f"include air={'yes' if include_air else 'no'}, overwrite={'yes' if overwrite else 'no'}."
        )

    # ------------------------------------------------------------------
    # Cloud listing, loading, deletion, and diagnostics
    # ------------------------------------------------------------------

    def request_list(self, player: Player, search: str = "") -> None:
        if not self.require_schematic_access(player):
            return
        if not self._require_database(player):
            return
        player_uuid = player.unique_id
        search = search.strip()[:128]

        def operation() -> list[dict[str, Any]]:
            assert self.store is not None
            return self.store.list(search=search, limit=50)

        def success(rows: list[dict[str, Any]]) -> None:
            current = self.server.get_player(player_uuid)
            if current and self.require_schematic_access(current):
                self.forms.show_library(current, rows, search)

        def failure(error: BaseException) -> None:
            current = self.server.get_player(player_uuid)
            if current:
                current.send_error_message(f"Unable to list schematics: {error}")

        self._submit_worker(operation, success, failure)
        player.send_message("§7Querying the shared schematic library...")

    def request_load(self, player: Player, name: str) -> None:
        if not self.require_schematic_access(player):
            return
        if not self._require_database(player):
            return
        try:
            normalized = normalize_schematic_name(name)
        except ValueError as exc:
            player.send_error_message(str(exc))
            return
        player_uuid = player.unique_id

        def operation() -> tuple[dict[str, Any], Any, dict[str, int | bool]]:
            assert self.store is not None
            payload_path = self._new_payload_path("payload-load-")
            decoded = None
            try:
                row = self.store.fetch_to_file(normalized, payload_path)
                self._ensure_stream_workspace(
                    self._paste_workspace_estimate(
                        int(row.get("block_count", 0)), int(row.get("compressed_bytes", 0))
                    ),
                    f"loading and planning '{normalized}'",
                )
                decoded = decode_schematic_file(
                    payload_path,
                    str(row["content_sha256"]),
                    self._new_record_buffer,
                )
                integrity = validate_schematic_integrity(decoded)
                if int(row["block_count"]) != int(integrity["block_count"]):
                    raise RuntimeError(
                        f"MySQL metadata says {int(row['block_count']):,} records but the payload contains "
                        f"{int(integrity['block_count']):,}"
                    )
                return row, decoded, integrity
            except Exception:
                if decoded is not None:
                    self._cleanup_decoded_schematic(decoded)
                raise
            finally:
                payload_path.unlink(missing_ok=True)

        def success(result: tuple[dict[str, Any], Any, dict[str, int | bool]]) -> None:
            row, decoded, integrity = result
            current = self.server.get_player(player_uuid)
            if not current or not self.require_schematic_access(current):
                self._cleanup_decoded_schematic(decoded)
                return
            location = current.location
            placement = PlacementSession(
                name=normalized,
                schematic=decoded,
                dimension_id=self.dimension_identifier(location.dimension),
                anchor=BlockPos(location.block_x, location.block_y, location.block_z),
                rotation=0,
                expires_at_tick=self._tick_counter + self._preview_duration_ticks,
            )
            previous = self.placements.pop(player_uuid, None)
            self._cleanup_placement(previous)
            self.placements[player_uuid] = placement
            backing = "disk-streamed" if getattr(decoded.records, "is_file_backed", False) else "memory"
            current.send_message(
                f"§aLoaded and integrity-checked '{normalized}' from MySQL: "
                f"{row['size_x']}×{row['size_y']}×{row['size_z']}, "
                f"{int(integrity['block_count']):,} stored records for "
                f"{int(integrity['volume']):,} volume blocks ({backing} record store). "
                "Use the placer/rotator or /schem commands."
            )
            self._spawn_preview(current, placement)
            self.forms.open_placement(current)

        def failure(error: BaseException) -> None:
            current = self.server.get_player(player_uuid)
            if current:
                current.send_error_message(f"Unable to load '{normalized}': {error}")

        self._submit_worker(operation, success, failure)
        player.send_message(f"§7Downloading and streaming '{normalized}' into a bounded-memory workspace...")

    def request_export_to_disk(
        self,
        player: Player,
        name: str,
        *,
        remove_from_mysql: bool = False,
        overwrite: bool | None = None,
    ) -> None:
        """Download one cloud schematic and atomically save its payload to disk."""

        if not self.require_schematic_access(player):
            return
        if not self._require_database(player) or not self._require_disk(player):
            return
        if remove_from_mysql and not self._hard_delete_enabled:
            player.send_error_message("Permanent MySQL removal is disabled in config.toml.")
            return
        try:
            normalized = normalize_schematic_name(name)
        except ValueError as exc:
            player.send_error_message(str(exc))
            return
        player_uuid = player.unique_id

        def operation() -> tuple[str, Path, bool]:
            assert self.store is not None
            assert self.disk_store is not None
            payload_path = self._new_payload_path("payload-export-")
            try:
                row = self.store.fetch_to_file(normalized, payload_path)
                destination = self.disk_store.save_cloud_file(
                    row, payload_path, overwrite=overwrite
                )
                if remove_from_mysql:
                    try:
                        self.store.hard_delete(normalized)
                    except Exception as exc:
                        raise RuntimeError(
                            f"disk backup was saved as '{destination.name}', but MySQL removal failed: {exc}"
                        ) from exc
                return normalized, destination, remove_from_mysql
            finally:
                payload_path.unlink(missing_ok=True)

        def success(result: tuple[str, Path, bool]) -> None:
            exported, destination, removed = result
            current = self.server.get_player(player_uuid)
            action = "saved to disk and permanently removed from MySQL" if removed else "saved to disk"
            if current and self.require_schematic_access(current, notify=False):
                current.send_message(f"§a'{exported}' was {action} as {destination.name}.")
            self.logger.info(f"Cloud schematic '{exported}' was {action}: {destination}")

        def failure(error: BaseException) -> None:
            current = self.server.get_player(player_uuid)
            if current:
                current.send_error_message(f"Cloud-to-disk operation failed: {error}")
            self.logger.error(f"Cloud-to-disk operation for '{normalized}' failed: {error}")

        self._submit_worker(operation, success, failure)
        if remove_from_mysql:
            player.send_message(f"§7Backing up '{normalized}' to disk before removing it from MySQL...")
        else:
            player.send_message(f"§7Saving cloud schematic '{normalized}' to disk...")

    def request_export_worldedit(
        self,
        player: Player,
        name: str,
        *,
        overwrite: bool | None = None,
    ) -> None:
        """Convert one cloud schematic to Sponge v3 for WorldEdit and Amulet."""

        if not self.require_schematic_access(player):
            return
        if not self._require_database(player) or not self._require_worldedit(player):
            return
        try:
            normalized = normalize_schematic_name(name)
        except ValueError as exc:
            player.send_error_message(str(exc))
            return
        player_uuid = player.unique_id

        def operation() -> tuple[str, Path, dict[str, Any]]:
            assert self.store is not None
            assert self.worldedit_store is not None
            row = self.store.fetch(normalized)
            decoded = decode_schematic(bytes(row["payload"]), str(row["content_sha256"]))
            payload, report = encode_sponge_v3(
                decoded,
                data_version=self.worldedit_store.settings.data_version,
                name=str(row.get("display_name") or normalized),
                author=str(row.get("author_name") or "Unknown"),
                plugin_version=PLUGIN_VERSION,
                compression_level=self._compression_level,
            )
            destination = self.worldedit_store.save(
                normalized, payload, report, overwrite=overwrite
            )
            return normalized, destination, report.as_dict()

        def success(result: tuple[str, Path, dict[str, Any]]) -> None:
            exported, destination, report = result
            current = self.server.get_player(player_uuid)
            warning_count = len(report.get("warnings", []))
            stripped_count = sum(len(values) for values in report.get("stripped_states", {}).values())
            suffix = ""
            if warning_count or stripped_count:
                suffix = (
                    f" Conversion report: {warning_count} warning(s), "
                    f"{stripped_count} stripped Bedrock state name(s)."
                )
            if current and self.require_schematic_access(current, notify=False):
                current.send_message(
                    f"§aExported '{exported}' as Sponge v3: {destination.name}." + suffix
                )
            self.logger.info(
                f"Exported cloud schematic '{exported}' for WorldEdit/Amulet: {destination}; "
                f"warnings={warning_count}, stripped_states={stripped_count}"
            )

        def failure(error: BaseException) -> None:
            current = self.server.get_player(player_uuid)
            if current:
                current.send_error_message(f"WorldEdit/Amulet export failed: {error}")
            self.logger.error(f"WorldEdit/Amulet export for '{normalized}' failed: {error}")

        self._submit_worker(operation, success, failure)
        player.send_message(
            f"§7Converting cloud schematic '{normalized}' to Sponge v3 off-thread..."
        )

    def request_remove_from_mysql(self, player: Player, name: str) -> None:
        """Permanently delete one schematic from MySQL after UI confirmation."""

        if not self.require_schematic_access(player):
            return
        if not self._require_database(player):
            return
        if not self._hard_delete_enabled:
            player.send_error_message("Permanent MySQL removal is disabled in config.toml.")
            return
        try:
            normalized = normalize_schematic_name(name)
        except ValueError as exc:
            player.send_error_message(str(exc))
            return
        player_uuid = player.unique_id

        def operation() -> str:
            assert self.store is not None
            self.store.hard_delete(normalized)
            return normalized

        def success(deleted: str) -> None:
            current = self.server.get_player(player_uuid)
            if current and self.require_schematic_access(current, notify=False):
                current.send_message(f"§aPermanently removed '{deleted}' from MySQL.")
            self.logger.info(f"Permanently removed cloud schematic '{deleted}' from MySQL.")

        def failure(error: BaseException) -> None:
            current = self.server.get_player(player_uuid)
            if current:
                current.send_error_message(f"MySQL removal failed: {error}")

        self._submit_worker(operation, success, failure)

    def request_archive(self, player: Player, name: str) -> None:
        """Keep the row in MySQL but hide it from the active cloud library."""

        if not self.require_schematic_access(player):
            return
        if not self._require_database(player):
            return
        try:
            normalized = normalize_schematic_name(name)
        except ValueError as exc:
            player.send_error_message(str(exc))
            return
        player_uuid = player.unique_id

        def operation() -> str:
            assert self.store is not None
            self.store.soft_delete(normalized)
            return normalized

        def success(archived: str) -> None:
            current = self.server.get_player(player_uuid)
            if current and self.require_schematic_access(current, notify=False):
                current.send_message(f"§aArchived '{archived}' from the active cloud library.")

        def failure(error: BaseException) -> None:
            current = self.server.get_player(player_uuid)
            if current:
                current.send_error_message(f"Archive failed: {error}")

        self._submit_worker(operation, success, failure)

    def request_delete(self, player: Player, name: str) -> None:
        """Backward-compatible alias for permanent MySQL removal."""

        self.request_remove_from_mysql(player, name)

    def request_db_test(self, sender: CommandSender) -> None:
        if isinstance(sender, Player) and not self.require_schematic_access(sender):
            return
        if self.store is None:
            sender.send_error_message(f"Schematic database is unavailable: {self.db_error}")
            return

        def operation() -> bool:
            assert self.store is not None
            if self.store.settings.auto_create_schema:
                self.store.ensure_schema()
            self.store.ping()
            return True

        def success(_result: Any) -> None:
            self.db_ready = True
            self.db_error = ""
            sender.send_message("§aMySQL schematic database health check passed.")

        def failure(error: BaseException) -> None:
            self.db_ready = False
            self.db_error = str(error)
            sender.send_error_message(f"MySQL health check failed: {error}")

        self._submit_worker(operation, success, failure)
        sender.send_message("§7Running MySQL health check off-thread...")

    # ------------------------------------------------------------------
    # Selection particle preview
    # ------------------------------------------------------------------

    def _refresh_selection_previews(self) -> None:
        for player_uuid, selection in list(self.selections.items()):
            if not selection.complete or selection.dimension_id is None:
                continue
            player = self.server.get_player(player_uuid)
            if player is not None and self.has_schematic_access(player):
                self._spawn_selection_preview(player, selection)

    def _spawn_selection_preview(self, player: Player, selection: Selection) -> None:
        if not self._selection_preview_enabled or not selection.complete or selection.dimension_id is None:
            return
        if self.dimension_identifier(player.location.dimension) != selection.dimension_id:
            return
        low, _high = selection.bounds()
        try:
            for x, y, z in self._box_points(low, selection.size, self._selection_points):
                player.spawn_particle(self._selection_particle, x, y, z)
        except Exception as exc:
            self.logger.debug(f"Selection preview particle failed for {player.name}: {exc}")

    def clear_selection(self, player: Player) -> None:
        if not self.require_schematic_access(player):
            return
        if self.selections.pop(player.unique_id, None) is None:
            player.send_message("§7You do not have an active selection.")
            return
        player.send_message("§eSelection cleared.")

    # ------------------------------------------------------------------
    # Placement, preview, rotation, and paste operations
    # ------------------------------------------------------------------

    def placement_size(self, placement: PlacementSession) -> tuple[int, int, int]:
        return rotated_size(placement.schematic.size, placement.rotation)

    def anchor_from_clicked_block(self, player: Player, block: Any, block_face: Any) -> None:
        if not self.require_schematic_access(player):
            return
        placement = self.placements.get(player.unique_id)
        if not placement:
            player.send_error_message("Load a schematic before setting its anchor.")
            return
        face_name = getattr(block_face, "name", str(block_face)).lower().split(".")[-1]
        offset = {
            "down": (0, -1, 0),
            "up": (0, 1, 0),
            "north": (0, 0, -1),
            "south": (0, 0, 1),
            "west": (-1, 0, 0),
            "east": (1, 0, 0),
        }.get(face_name, (0, 0, 0))
        placement.dimension_id = self.dimension_identifier(block.dimension)
        placement.anchor = BlockPos(block.x + offset[0], block.y + offset[1], block.z + offset[2])
        self.refresh_preview(player)
        player.send_message(
            f"§bPlacement anchor: {placement.anchor.x}, {placement.anchor.y}, {placement.anchor.z}."
        )

    def anchor_at_player(self, player: Player) -> None:
        if not self.require_schematic_access(player):
            return
        placement = self.placements.get(player.unique_id)
        if not placement:
            player.send_error_message("There is no active placement.")
            return
        location = player.location
        placement.dimension_id = self.dimension_identifier(location.dimension)
        placement.anchor = BlockPos(location.block_x, location.block_y, location.block_z)
        self.refresh_preview(player)
        player.send_message("§bPlacement anchor moved to your feet.")

    def set_anchor(self, player: Player, coordinates: list[int]) -> None:
        if not self.require_schematic_access(player):
            return
        placement = self.placements.get(player.unique_id)
        if not placement:
            player.send_error_message("There is no active placement.")
            return
        placement.dimension_id = self.dimension_identifier(player.location.dimension)
        placement.anchor = BlockPos(*coordinates)
        self.refresh_preview(player)
        player.send_message(f"§bPlacement anchor set to {coordinates[0]}, {coordinates[1]}, {coordinates[2]}.")

    def rotate_placement(self, player: Player, value: int, absolute: bool = False) -> None:
        if not self.require_schematic_access(player):
            return
        placement = self.placements.get(player.unique_id)
        if not placement:
            player.send_error_message("There is no active placement to rotate.")
            return
        try:
            placement.rotation = normalize_rotation(value if absolute else placement.rotation + value)
        except ValueError as exc:
            player.send_error_message(str(exc))
            return
        self.refresh_preview(player)
        size = self.placement_size(placement)
        player.send_message(f"§dRotation: {placement.rotation}° | size {size[0]}×{size[1]}×{size[2]}.")

    def refresh_preview(self, player: Player) -> None:
        if not self.require_schematic_access(player):
            return
        placement = self.placements.get(player.unique_id)
        if not placement:
            player.send_error_message("There is no active placement.")
            return
        placement.expires_at_tick = self._tick_counter + self._preview_duration_ticks
        self._spawn_preview(player, placement)

    def _refresh_active_previews(self) -> None:
        expired: list[Any] = []
        for player_uuid, placement in list(self.placements.items()):
            if placement.expires_at_tick < self._tick_counter:
                expired.append(player_uuid)
                continue
            player = self.server.get_player(player_uuid)
            if player and self.has_schematic_access(player):
                self._spawn_preview(player, placement)
        for player_uuid in expired:
            self._cleanup_placement(self.placements.pop(player_uuid, None))

    def _spawn_preview(self, player: Player, placement: PlacementSession) -> None:
        if not self._preview_enabled:
            return
        if self.dimension_identifier(player.location.dimension) != placement.dimension_id:
            return
        size = self.placement_size(placement)
        try:
            for x, y, z in self._box_points(placement.anchor, size, self._preview_points):
                player.spawn_particle(self._preview_particle, x, y, z)
        except Exception as exc:
            # Preview is cosmetic. Avoid killing placement workflow if a particle name is invalid.
            self.logger.debug(f"Preview particle failed for {player.name}: {exc}")

    @staticmethod
    def _box_points(anchor: BlockPos, size: tuple[int, int, int], points_per_edge: int):
        x0, y0, z0 = float(anchor.x), float(anchor.y), float(anchor.z)
        x1, y1, z1 = x0 + size[0], y0 + size[1], z0 + size[2]
        seen: set[tuple[float, float, float]] = set()

        def values(start: float, end: float):
            if points_per_edge <= 1 or math.isclose(start, end):
                yield start
                return
            for i in range(points_per_edge):
                yield start + (end - start) * i / (points_per_edge - 1)

        for x in values(x0, x1):
            for y, z in ((y0, z0), (y0, z1), (y1, z0), (y1, z1)):
                seen.add((x, y, z))
        for y in values(y0, y1):
            for x, z in ((x0, z0), (x0, z1), (x1, z0), (x1, z1)):
                seen.add((x, y, z))
        for z in values(z0, z1):
            for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1)):
                seen.add((x, y, z))
        yield from seen

    def start_paste(self, player: Player) -> None:
        if not self.require_schematic_access(player):
            return
        if (
            player.unique_id in self.paste_jobs
            or player.unique_id in self.save_jobs
            or player.unique_id in self.preparing_pastes
        ):
            player.send_error_message("You already have an active schematic job.")
            return
        placement = self.placements.pop(player.unique_id, None)
        if not placement:
            player.send_error_message("Load and position a schematic before pasting.")
            return

        player_uuid = player.unique_id
        token = object()
        self.preparing_pastes[player_uuid] = (token, placement)

        def operation() -> Any:
            if self._streaming_enabled:
                return prepare_streaming_paste_plan(
                    placement.schematic,
                    placement.anchor,
                    placement.rotation,
                    self._new_record_buffer,
                    batch_records=self._plan_batch_records,
                )
            return prepare_paste_plan(placement.schematic, placement.anchor, placement.rotation)

        def success(plan: Any) -> None:
            pending = self.preparing_pastes.get(player_uuid)
            if pending is None or pending[0] is not token:
                self._cleanup_plan(plan)
                self._cleanup_placement(placement)
                return
            self.preparing_pastes.pop(player_uuid, None)
            current = self.server.get_player(player_uuid)
            if not current:
                self._cleanup_plan(plan)
                self._cleanup_placement(placement)
                return
            capture_history = (
                self._history_enabled
                and plan.block_count <= self._history_max_blocks_per_operation
            )
            history_reason = ""
            if self._history_enabled and not capture_history:
                history_reason = (
                    f"paste contains {plan.block_count:,} records, above history.max_blocks_per_operation="
                    f"{self._history_max_blocks_per_operation:,}"
                )
            job = PasteJob(
                player_uuid=player_uuid,
                name=placement.name,
                plan=plan,
                dimension_id=placement.dimension_id,
                anchor=placement.anchor,
                rotation=placement.rotation,
                operation="paste",
                capture_history=capture_history,
                history_disabled_reason=history_reason,
                before_records=(self._new_record_buffer("records-history-before-") if capture_history else bytearray()),
                after_records=(self._new_record_buffer("records-history-after-") if capture_history else bytearray()),
                started_tick=self._tick_counter,
                last_progress_tick=self._tick_counter,
            )
            self._cleanup_placement(placement)
            self.paste_jobs[player_uuid] = job
            plan_backing = "disk-streamed" if getattr(plan.records, "is_file_backed", False) else "memory"
            history_note = "" if capture_history else (f" Undo disabled: {history_reason}." if history_reason else "")
            current.send_message(
                f"§aStarted chunk-aware paste of '{job.name}': {plan.block_count:,} blocks across "
                f"{len(plan.chunks):,} chunk range(s), up to {self._paste_budget:,} blocks or "
                f"{getattr(self, '_paste_time_budget_seconds', 0.010) * 1000:g} ms per tick "
                f"using a {plan_backing} plan.{history_note}"
            )

        def failure(error: BaseException) -> None:
            pending = self.preparing_pastes.get(player_uuid)
            if pending is None or pending[0] is not token:
                self._cleanup_placement(placement)
                return
            self.preparing_pastes.pop(player_uuid, None)
            current = self.server.get_player(player_uuid)
            if current:
                self.placements[player_uuid] = placement
                current.send_error_message(f"Unable to prepare schematic paste: {error}")
            else:
                self._cleanup_placement(placement)
            self.logger.error(f"Paste plan for '{placement.name}' failed: {error}")

        self._submit_worker(operation, success, failure)
        player.send_message(
            f"§7Preparing a bounded-memory chunk paste plan for '{placement.name}' off-thread. "
            "Large plans spill to disk; the preview is paused until preparation finishes."
        )

    def start_history_action(self, player: Player, operation: str) -> None:
        operation = operation.lower()
        if operation not in {"undo", "redo"}:
            player.send_error_message("History operation must be undo or redo.")
            return
        if not self._history_enabled:
            player.send_error_message("Undo and redo are disabled in config.toml.")
            return
        if not self.require_schematic_access(player):
            return
        if (
            player.unique_id in self.paste_jobs
            or player.unique_id in self.save_jobs
            or player.unique_id in self.preparing_pastes
        ):
            player.send_error_message("Finish or cancel the active schematic job first.")
            return
        source = self.undo_history if operation == "undo" else self.redo_history
        stack = source.get(player.unique_id, [])
        if not stack:
            player.send_message(f"§7There is nothing to {operation}.")
            return
        entry = stack[-1]
        plan = entry.before_plan if operation == "undo" else entry.after_plan
        job = PasteJob(
            player_uuid=player.unique_id,
            name=entry.name,
            plan=plan,
            dimension_id=entry.dimension_id,
            anchor=entry.anchor,
            rotation=0,
            operation=operation,
            history_entry=entry,
            capture_history=False,
            started_tick=self._tick_counter,
            last_progress_tick=self._tick_counter,
        )
        self.paste_jobs[player.unique_id] = job
        player.send_message(
            f"§dStarted {operation} for '{entry.name}': {entry.block_count:,} changed blocks across "
            f"{len(plan.chunks):,} chunk(s), up to {self._paste_budget:,} blocks or "
            f"{getattr(self, '_paste_time_budget_seconds', 0.010) * 1000:g} ms per tick."
        )

    def undo(self, player: Player) -> None:
        self.start_history_action(player, "undo")

    def redo(self, player: Player) -> None:
        self.start_history_action(player, "redo")

    def cancel_placement(self, player: Player) -> None:
        placement = self.placements.pop(player.unique_id, None)
        if placement:
            self._cleanup_placement(placement)
            player.send_message("§ePlacement preview cancelled.")
        else:
            player.send_error_message("There is no active placement.")

    def handle_player_quit(self, player: Player) -> None:
        """Release interactive state without aborting an active world operation.

        A Bedrock client can disconnect during a lag spike while the server and its
        scheduled paste are still alive. Save/paste jobs contain no live Player
        references, so keeping them running is both safe and prevents a disconnect from
        turning a temporary networking problem into a permanently partial build.
        """

        player_uuid = player.unique_id
        placement = self.placements.pop(player_uuid, None)
        self._cleanup_placement(placement)

        pending = self.preparing_pastes.pop(player_uuid, None)
        if isinstance(pending, tuple) and len(pending) > 1:
            self._cleanup_placement(pending[1])

        active = self.paste_jobs.get(player_uuid) or self.save_jobs.get(player_uuid)
        if active is not None:
            self.logger.info(
                f"Player disconnected; continuing active schematic "
                f"{getattr(active, 'operation', 'save')} '{active.name}'."
            )

    def cancel_player_jobs(self, player: Player, notify: bool = True) -> bool:
        cancelled = False
        save_job = self.save_jobs.pop(player.unique_id, None)
        if save_job is not None:
            self._release_job_chunk(save_job, release_slot=True)
            self._close_record_storage(save_job.records)
            cancelled = True
        paste_job = self.paste_jobs.pop(player.unique_id, None)
        if paste_job is not None:
            self._release_job_chunk(paste_job, release_slot=True)
            if paste_job.operation == "paste":
                partial = self._history_entry_from_job(paste_job)
                if partial is not None:
                    self._push_undo_history(player.unique_id, partial, clear_redo=True)
                    if notify:
                        player.send_message(
                            f"§ePartial paste history saved for {partial.block_count:,} changed blocks."
                        )
                self._cleanup_plan(paste_job.plan)
                self._close_record_storage(paste_job.before_records)
                self._close_record_storage(paste_job.after_records)
            cancelled = True
        pending = self.preparing_pastes.pop(player.unique_id, None)
        if pending is not None:
            if isinstance(pending, tuple) and len(pending) > 1:
                self._cleanup_placement(pending[1])
            cancelled = True
        if notify:
            if cancelled:
                player.send_message("§eActive schematic job cancelled.")
            else:
                player.send_message("§7You do not have an active scan, preparation, or paste job.")
        return cancelled

    def cancel_all(self, player: Player, notify: bool = True) -> None:
        cancelled = self.cancel_player_jobs(player, notify=False)
        placement = self.placements.pop(player.unique_id, None)
        if placement is not None:
            self._cleanup_placement(placement)
            cancelled = True
        if notify:
            if cancelled:
                player.send_message("§eActive schematic scan, paste, or placement cancelled.")
            else:
                player.send_message("§7You do not have an active schematic job or placement.")

    # ------------------------------------------------------------------
    # Tool items and status
    # ------------------------------------------------------------------

    @staticmethod
    def dimension_identifier(dimension: Any) -> str:
        return get_dimension_identifier(dimension)

    def _get_dimension(self, identifier: str) -> Any | None:
        return resolve_dimension(self.server.level, identifier)

    @staticmethod
    def _identifier(value: Any) -> str:
        identifier = getattr(value, "id", value)
        return str(identifier)

    def item_identifier(self, item: Any) -> str:
        return self._identifier(item.type)

    def block_data_identifier(self, data: Any) -> str:
        return self._identifier(data.type)

    def give_tools(self, player: Player) -> None:
        if not self.require_schematic_access(player):
            return
        try:
            leftovers = []
            for item_id in self.tool_ids.values():
                leftovers.extend(player.inventory.add_item(ItemStack(item_id, 1)))
            player.send_message(
                "§aAdded the selector, placer, rotator, cloud tablet, undo, redo, and confirm tools."
            )
            if leftovers:
                player.send_message("§eYour inventory was full; one or more tool items were dropped or not added.")
        except Exception as exc:
            player.send_error_message(
                f"Unable to create schematic tools: {exc}. Ensure both add-on packs are active on this world."
            )

    def status_text(self, player: Player) -> str:
        disk_ready = self.disk_store is not None and self.disk_store.settings.enabled
        disk_status = "§aReady" if disk_ready else "§cUnavailable"
        disk_path = str(self.disk_store.root) if self.disk_store is not None else getattr(self, "disk_error", "disabled")
        lines = [
            "§l§bNinj-OS Schematic Cloud Status§r",
            f"§7Database: {'§aConnected' if self.db_ready else '§cUnavailable'}§r",
            f"§7Disk backups: {disk_status} §8({disk_path})§r",
            f"§7Access: §foperator or tag {self._architect_tag}",
            f"§7Large-schematic streaming: §f{'enabled' if self._streaming_enabled else 'disabled'} "
            f"(spill after {self._record_spill_threshold // (1024**2)} MiB)",
            f"§7Streaming workspace: §f{self._stream_work_dir}",
        ]
        blockdata = getattr(self, "_blockdata", None)
        if blockdata is not None:
            lines.append(
                f"§7BlockData retention: §aReady §8(API {blockdata.api_version}, "
                f"{blockdata.adapter_name})§r"
            )
        else:
            blockdata_error = str(
                getattr(self, "_blockdata_error", "not initialized")
            )[:180]
            lines.append(
                f"§7BlockData retention: §cUnavailable §8("
                f"{blockdata_error})§r"
            )
        selection = self.selections.get(player.unique_id)
        if selection and selection.complete:
            sx, sy, sz = selection.size
            lines.append(f"§7Selection: §f{sx}×{sy}×{sz} ({selection.volume:,})")
        else:
            lines.append("§7Selection: §fIncomplete")
        placement = self.placements.get(player.unique_id)
        lines.append(
            f"§7Placement: §f{placement.name} at {placement.rotation}°" if placement else "§7Placement: §fNone"
        )
        save = self.save_jobs.get(player.unique_id)
        paste = self.paste_jobs.get(player.unique_id)
        preparing = self.preparing_pastes.get(player.unique_id)
        if save:
            lines.append(f"§7Save scan: §f{save.name} {save.cursor * 100 / save.total_volume:.1f}%")
        if preparing:
            lines.append(f"§7Paste preparation: §f{preparing[1].name}")
        if paste:
            lines.append(
                f"§7{paste.operation.title()}: §f{paste.name} "
                f"{paste.cursor * 100 / max(1, paste.plan.block_count):.1f}%"
            )
        if not save and not preparing and not paste:
            lines.append("§7Active job: §fNone")
        lines.append(f"§7Undo history: §f{len(self.undo_history.get(player.unique_id, []))}")
        lines.append(f"§7Redo history: §f{len(self.redo_history.get(player.unique_id, []))}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Command interface
    # ------------------------------------------------------------------

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "schem":
            return False
        raw = " ".join(args).strip()
        try:
            tokens = shlex.split(raw) if raw else []
        except ValueError as exc:
            sender.send_error_message(f"Invalid command quoting: {exc}")
            return True
        subcommand = tokens[0].lower() if tokens else "menu"
        rest = tokens[1:]

        if isinstance(sender, Player):
            if not self.require_schematic_access(sender):
                return True
            player = sender
        else:
            if subcommand not in {"version", "ver", "dbtest"}:
                sender.send_error_message(
                    "This schematic command requires an authorized in-game player, "
                    "except /schem dbtest and /schem version from the console."
                )
                return True
            player = None

        if subcommand in {"help", "?"}:
            sender.send_message(self._help_text())
            return True
        if subcommand in {"version", "ver"}:
            sender.send_message(
                f"§bNinj-OS Schematics v{PLUGIN_VERSION} §7build={BUILD_ID} "
                f"access=operator-or-tag:{self._architect_tag} module={Path(__file__).resolve()}"
            )
            return True
        if subcommand == "dbtest":
            self.request_db_test(sender)
            return True
        assert player is not None

        try:
            if subcommand in {"menu", "ui", "form"}:
                self.forms.open_main(player)
            elif subcommand in {"pos1", "p1"}:
                self.set_selection_at_player(player, 1, self._parse_coordinates(rest) if rest else None)
            elif subcommand in {"pos2", "p2"}:
                self.set_selection_at_player(player, 2, self._parse_coordinates(rest) if rest else None)
            elif subcommand in {"selection", "sel"}:
                player.send_message(self.status_text(player))
            elif subcommand in {"clearselection", "clearsel"}:
                self.clear_selection(player)
            elif subcommand == "save":
                if not rest:
                    self.forms.open_save(player)
                else:
                    include_air = self._parse_bool(rest[1]) if len(rest) >= 2 else None
                    overwrite = self._parse_bool(rest[2]) if len(rest) >= 3 else None
                    self.start_save(player, rest[0], "", include_air, overwrite)
            elif subcommand in {"list", "browse"}:
                self.request_list(player, " ".join(rest))
            elif subcommand == "load":
                if not rest:
                    player.send_error_message("Usage: /schem load <name>")
                else:
                    self.request_load(player, rest[0])
            elif subcommand in {"export", "download", "disk-save"}:
                if not rest:
                    player.send_error_message("Usage: /schem export <cloud-name> [overwrite]")
                else:
                    overwrite = self._parse_bool(rest[1]) if len(rest) >= 2 else None
                    self.request_export_to_disk(player, rest[0], overwrite=overwrite)
            elif subcommand in {"export-worldedit", "worldedit", "amulet", "sponge-v3"}:
                if not rest:
                    player.send_error_message(
                        "Usage: /schem export-worldedit <cloud-name> [overwrite]"
                    )
                else:
                    overwrite = self._parse_bool(rest[1]) if len(rest) >= 2 else None
                    self.request_export_worldedit(player, rest[0], overwrite=overwrite)
            elif subcommand == "worldeditpath":
                if self._require_worldedit(player):
                    assert self.worldedit_store is not None
                    player.send_message(
                        f"§bWorldEdit/Amulet export folder: §f{self.worldedit_store.root}"
                    )
            elif subcommand in {"remove", "delete"}:
                if not rest:
                    player.send_error_message("Usage: /schem remove <name>")
                else:
                    self.request_remove_from_mysql(player, rest[0])
            elif subcommand == "archive":
                if not rest:
                    player.send_error_message("Usage: /schem archive <name>")
                else:
                    self.request_archive(player, rest[0])
            elif subcommand in {"backup-remove", "export-remove"}:
                if not rest:
                    player.send_error_message("Usage: /schem backup-remove <name> [overwrite]")
                else:
                    overwrite = self._parse_bool(rest[1]) if len(rest) >= 2 else None
                    self.request_export_to_disk(
                        player, rest[0], remove_from_mysql=True, overwrite=overwrite
                    )
            elif subcommand == "diskpath":
                if self._require_disk(player):
                    assert self.disk_store is not None
                    player.send_message(f"§bSchematic disk folder: §f{self.disk_store.root}")
            elif subcommand == "anchor":
                if rest:
                    self.set_anchor(player, self._parse_coordinates(rest))
                else:
                    self.anchor_at_player(player)
            elif subcommand == "rotate":
                if not rest or rest[0].lower() in {"cw", "right", "+"}:
                    self.rotate_placement(player, 90, absolute=False)
                elif rest[0].lower() in {"ccw", "left", "-"}:
                    self.rotate_placement(player, -90, absolute=False)
                else:
                    self.rotate_placement(player, int(rest[0]), absolute=True)
            elif subcommand == "preview":
                self.refresh_preview(player)
            elif subcommand in {"paste", "place"}:
                self.forms.open_paste_confirmation(player)
            elif subcommand in {"confirm", "commit"}:
                self.start_paste(player)
            elif subcommand == "undo":
                self.undo(player)
            elif subcommand == "redo":
                self.redo(player)
            elif subcommand == "tools":
                self.give_tools(player)
            elif subcommand == "cancel":
                self.cancel_all(player, notify=True)
            elif subcommand == "status":
                player.send_message(self.status_text(player))
            else:
                player.send_error_message(f"Unknown schematic subcommand '{subcommand}'. Use /schem help.")
        except (ValueError, TypeError) as exc:
            player.send_error_message(str(exc))
        return True

    @staticmethod
    def _parse_coordinates(tokens: list[str]) -> list[int]:
        if len(tokens) != 3:
            raise ValueError("Coordinates require exactly three integers: <x> <y> <z>.")
        try:
            return [int(token) for token in tokens]
        except ValueError as exc:
            raise ValueError("Coordinates must be integers.") from exc

    @staticmethod
    def _parse_bool(value: str) -> bool:
        lowered = value.lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
        raise ValueError(f"Expected true/false, got '{value}'.")

    @staticmethod
    def _help_text() -> str:
        return (
            "§l§bNinj-OS Schematic Cloud§r\n"
            "§f/schem menu§7 - open the full form UI\n"
            "§f/schem pos1 [x y z]§7 - set corner one\n"
            "§f/schem pos2 [x y z]§7 - set corner two\n"
            "§f/schem clearselection§7 - clear the active selection outline\n"
            "§f/schem save <name> [include_air] [overwrite]§7 - scan and upload\n"
            "§f/schem list [search]§7 - browse shared blueprints\n"
            "§f/schem load <name>§7 - download and preview\n"
            "§f/schem export <name> [overwrite]§7 - save the native cloud payload to disk\n"
            "§f/schem export-worldedit <name> [overwrite]§7 - create Sponge v3 .schem for WorldEdit/Amulet\n"
            "§f/schem worldeditpath§7 - show the Sponge .schem export folder\n"
            "§f/schem backup-remove <name> [overwrite]§7 - back up to disk, then remove from MySQL\n"
            "§f/schem remove <name>§7 - permanently remove a cloud schematic from MySQL\n"
            "§f/schem archive <name>§7 - hide a schematic while retaining its MySQL row\n"
            "§f/schem diskpath§7 - show the configured backup folder\n"
            "§f/schem anchor [x y z]§7 - move the placement anchor\n"
            "§f/schem rotate [0|90|180|270|cw|ccw]§7 - rotate placement\n"
            "§f/schem preview§7 - refresh the particle bounding box\n"
            "§f/schem paste§7 - open the placement confirmation\n"
            "§f/schem confirm§7 - confirm and begin the tick-batched paste\n"
            "§f/schem undo§7 - undo the last completed or partial paste\n"
            "§f/schem redo§7 - redo the last undone paste\n"
            "§f/schem tools§7 - receive the add-on tools\n"
            "§f/schem status§7 - show current jobs\n"
            "§f/schem cancel§7 - cancel scan, paste, and preview\n"
            "§f/schem dbtest§7 - test the remote MySQL connection\n"
            "§f/schem version§7 - show the loaded build and module path"
        )
