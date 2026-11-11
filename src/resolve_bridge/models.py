"""Immutable JSON-compatible values; no native Resolve objects cross this boundary."""

from dataclasses import asdict, dataclass
from typing import Literal

TrackType = Literal["video", "audio"]


@dataclass(frozen=True)
class ConnectionInfo:
    resolve_version: str
    product_name: str
    scripting_module_path: str
    loading_method: str


@dataclass(frozen=True)
class FrameRate:
    value: float
    numerator: int
    denominator: int
    drop_frame: bool
    raw: str


@dataclass(frozen=True)
class ProjectState:
    id: str
    name: str


@dataclass(frozen=True)
class TimelineItemState:
    id: str
    name: str
    media_id: str | None
    track_type: TrackType
    track_index: int
    start_frame: int
    end_frame: int
    duration_frames: int
    source_start_frame: int | None = None
    source_end_frame: int | None = None


@dataclass(frozen=True)
class TrackState:
    type: TrackType
    index: int
    name: str
    items: tuple[TimelineItemState, ...]
    locked: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class MarkerState:
    offset_frame: int
    color: str
    name: str
    note: str
    duration_frames: int
    custom_data: str


@dataclass(frozen=True)
class TimelineState:
    id: str
    name: str
    fps: float
    frame_rate: FrameRate
    start_frame: int
    end_frame: int
    start_timecode: str
    tracks: tuple[TrackState, ...]
    markers: tuple[MarkerState, ...] = ()


@dataclass(frozen=True)
class ResolveState:
    schema_version: int
    connection: ConnectionInfo
    project: ProjectState
    timeline: TimelineState

    def to_dict(self) -> dict:
        return {**asdict(self), "snapshot_id": self.snapshot_id}

    @property
    def snapshot_id(self) -> str:
        from .snapshot import snapshot_id
        return snapshot_id(self)


@dataclass(frozen=True)
class BinState:
    id: str
    name: str
    parent_id: str | None
    path: tuple[str, ...]


@dataclass(frozen=True)
class MediaItemState:
    id: str
    name: str
    bin_id: str
    bin_path: tuple[str, ...]
    file_name: str | None
    source_path: str | None
    media_type: str | None
    duration_frames: int | None
    frame_rate: FrameRate | None
    warnings: tuple[str, ...] = ()
    source_start_frame: int | None = None
    online_status: str | None = None


@dataclass(frozen=True)
class MediaInventory:
    schema_version: int
    project: ProjectState
    bins: tuple[BinState, ...]
    items: tuple[MediaItemState, ...]

    def to_dict(self) -> dict:
        return asdict(self)
