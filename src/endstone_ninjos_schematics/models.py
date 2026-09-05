"""Pure data models used by the plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class BlockPos:
    x: int
    y: int
    z: int


@dataclass(slots=True)
class Selection:
    dimension_id: str | None = None
    pos1: BlockPos | None = None
    pos2: BlockPos | None = None

    @property
    def complete(self) -> bool:
        return self.dimension_id is not None and self.pos1 is not None and self.pos2 is not None

    def set_position(self, which: int, dimension_id: str, pos: BlockPos) -> None:
        if self.dimension_id is not None and self.dimension_id != dimension_id:
            self.pos1 = None
            self.pos2 = None
        self.dimension_id = dimension_id
        if which == 1:
            self.pos1 = pos
        elif which == 2:
            self.pos2 = pos
        else:
            raise ValueError("which must be 1 or 2")

    def bounds(self) -> tuple[BlockPos, BlockPos]:
        if not self.complete or self.pos1 is None or self.pos2 is None:
            raise ValueError("selection is incomplete")
        low = BlockPos(
            min(self.pos1.x, self.pos2.x),
            min(self.pos1.y, self.pos2.y),
            min(self.pos1.z, self.pos2.z),
        )
        high = BlockPos(
            max(self.pos1.x, self.pos2.x),
            max(self.pos1.y, self.pos2.y),
            max(self.pos1.z, self.pos2.z),
        )
        return low, high

    @property
    def size(self) -> tuple[int, int, int]:
        low, high = self.bounds()
        return high.x - low.x + 1, high.y - low.y + 1, high.z - low.z + 1

    @property
    def volume(self) -> int:
        sx, sy, sz = self.size
        return sx * sy * sz


@dataclass(slots=True)
class DecodedSchematic:
    header: dict[str, Any]
    palette: list[dict[str, Any]]
    records: Any

    @property
    def size(self) -> tuple[int, int, int]:
        size = self.header["size"]
        return int(size[0]), int(size[1]), int(size[2])

    @property
    def block_count(self) -> int:
        return int(self.header["block_count"])


@dataclass(slots=True)
class PlacementSession:
    name: str
    schematic: DecodedSchematic
    dimension_id: str
    anchor: BlockPos
    rotation: int = 0
    expires_at_tick: int = 0


@dataclass(frozen=True, slots=True)
class ChunkRegion:
    """The part of a cuboid selection contained by one chunk."""

    chunk_x: int
    chunk_z: int
    min_x: int
    max_x: int
    min_z: int
    max_z: int
    min_y: int
    max_y: int

    @property
    def size_x(self) -> int:
        return self.max_x - self.min_x + 1

    @property
    def size_y(self) -> int:
        return self.max_y - self.min_y + 1

    @property
    def size_z(self) -> int:
        return self.max_z - self.min_z + 1

    @property
    def volume(self) -> int:
        return self.size_x * self.size_y * self.size_z


@dataclass(frozen=True, slots=True)
class PasteChunkRange:
    chunk_x: int
    chunk_z: int
    start: int
    end: int

    @property
    def block_count(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class PastePlan:
    size: tuple[int, int, int]
    palette: list[dict[str, Any]]
    records: Any
    chunks: tuple[PasteChunkRange, ...]

    @property
    def block_count(self) -> int:
        return len(self.records) // 16




@dataclass(slots=True)
class HistoryEntry:
    """One reversible world edit captured as before/after paste plans."""

    name: str
    dimension_id: str
    anchor: BlockPos
    before_plan: PastePlan
    after_plan: PastePlan
    block_count: int
    created_tick: int = 0

@dataclass(slots=True)
class SaveJob:
    player_uuid: Any
    player_name: str
    player_xuid: str
    name: str
    display_name: str
    description: str
    overwrite: bool
    include_air: bool
    dimension_id: str
    low: BlockPos
    size: tuple[int, int, int]
    total_volume: int
    regions: tuple[ChunkRegion, ...]
    cursor: int = 0
    region_index: int = 0
    region_cursor: int = 0
    non_air_count: int = 0
    palette_lookup: dict[str, int] = field(default_factory=dict)
    palette: list[dict[str, Any]] = field(default_factory=list)
    records: Any = field(default_factory=bytearray)
    started_tick: int = 0
    last_progress_tick: int = 0
    ticket_chunk: tuple[int, int] | None = None
    ticket_owned: bool = False
    ticket_backend: str | None = None
    ticket_name: str | None = None
    ticket_slot: int | None = None
    waiting_since_tick: int | None = None
    ready_since_tick: int | None = None
    region_snapshot_active: bool = False
    region_record_start: int = 0
    region_palette_start: int = 0
    region_non_air_start: int = 0
    region_job_cursor_start: int = 0
    verified_regions: int = 0
    chunk_retries: int = 0

    @property
    def current_region(self) -> ChunkRegion | None:
        if self.region_index >= len(self.regions):
            return None
        return self.regions[self.region_index]

    @property
    def region_remaining(self) -> int:
        region = self.current_region
        return 0 if region is None else region.volume - self.region_cursor

    @property
    def region_complete(self) -> bool:
        region = self.current_region
        return region is not None and self.region_cursor >= region.volume

    def advance_region(self) -> None:
        self.region_index += 1
        self.region_cursor = 0
        self.waiting_since_tick = None
        self.ready_since_tick = None
        self.region_snapshot_active = False

    def coordinates(self, count: int) -> Iterator[tuple[int, int, int, int, int, int]]:
        """Yield coordinates from the current chunk region without crossing into the next one."""
        region = self.current_region
        if region is None:
            return
        start = self.region_cursor
        end = min(region.volume, start + count)
        sx = region.size_x
        sz = region.size_z
        for index in range(start, end):
            local_x = index % sx
            rem = index // sx
            local_z = rem % sz
            local_y = rem // sz
            x = region.min_x + local_x
            y = region.min_y + local_y
            z = region.min_z + local_z
            yield x, y, z, x - self.low.x, y - self.low.y, z - self.low.z


@dataclass(slots=True)
class PasteJob:
    player_uuid: Any
    name: str
    plan: PastePlan
    dimension_id: str
    anchor: BlockPos
    rotation: int
    operation: str = "paste"
    history_entry: HistoryEntry | None = None
    capture_history: bool = False
    history_disabled_reason: str = ""
    before_palette_lookup: dict[Any, int] = field(default_factory=dict)
    before_palette: list[dict[str, Any]] = field(default_factory=list)
    before_records: Any = field(default_factory=bytearray)
    after_palette_lookup: dict[Any, int] = field(default_factory=dict)
    after_palette: list[dict[str, Any]] = field(default_factory=list)
    after_records: Any = field(default_factory=bytearray)
    history_chunks: list[PasteChunkRange] = field(default_factory=list)
    history_chunk: tuple[int, int] | None = None
    history_chunk_start: int = 0
    cursor: int = 0
    chunk_index: int = 0
    placed: int = 0
    skipped: int = 0
    failed: int = 0
    state_fallbacks: int = 0
    missing_blocks: int = 0
    missing_substitutions: int = 0
    palette_modes: dict[int, str] = field(default_factory=dict)
    missing_type_counts: dict[str, int] = field(default_factory=dict)
    started_tick: int = 0
    last_progress_tick: int = 0
    ticket_chunk: tuple[int, int] | None = None
    ticket_owned: bool = False
    ticket_backend: str | None = None
    ticket_name: str | None = None
    ticket_slot: int | None = None
    waiting_since_tick: int | None = None
    ready_since_tick: int | None = None

    @property
    def current_chunk(self) -> PasteChunkRange | None:
        if self.chunk_index >= len(self.plan.chunks):
            return None
        return self.plan.chunks[self.chunk_index]

    @property
    def chunk_remaining(self) -> int:
        chunk = self.current_chunk
        return 0 if chunk is None else chunk.end - self.cursor

    @property
    def chunk_complete(self) -> bool:
        chunk = self.current_chunk
        return chunk is not None and self.cursor >= chunk.end

    @property
    def captured_blocks(self) -> int:
        return len(self.before_records) // 16

    def advance_chunk(self) -> None:
        self.chunk_index += 1
        self.waiting_since_tick = None
        self.ready_since_tick = None
