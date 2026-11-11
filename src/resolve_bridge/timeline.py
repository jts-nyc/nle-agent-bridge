"""Native objects stay inside this adapter; only normalized values leave it."""

from typing import Any

from ._api import _boolean, _call, _id, _integer, _text
from .errors import (
    ContextChangedError, NoActiveTimelineError, NoProjectError,
    UnexpectedAPIResponseError,
)
from .models import ConnectionInfo, MarkerState, ProjectState, ResolveState, TimelineItemState, TimelineState, TrackState, TrackType
from .timecode import parse_frame_rate


class ResolveAdapter:
    """Normalized read surface over Resolve 21's documented API."""

    def __init__(self, resolve: Any, module_path: str, loading_method: str):
        self._resolve = resolve
        self._module_path = module_path
        self._loading_method = loading_method

    def _project(self) -> Any:
        manager = _call(self._resolve, "GetProjectManager")
        if manager is None:
            raise UnexpectedAPIResponseError("GetProjectManager returned no manager.")
        project = _call(manager, "GetCurrentProject")
        if project is None:
            raise NoProjectError("No project is open in Resolve. Open a disposable test project, then retry.")
        return project

    def _context(self) -> tuple[Any, Any]:
        project = self._project()
        timeline = _call(project, "GetCurrentTimeline")
        if timeline is None:
            raise NoActiveTimelineError("The current project has no active timeline. Select a test timeline, then retry.")
        return project, timeline

    def read_media(self):
        """Inventory the entire Media Pool without selecting bins or timelines."""
        from .media import _read_inventory
        project = self._project()
        state = _read_inventory(project)
        if _id(self._project()) != state.project.id:
            raise ContextChangedError("The active project changed during the media read.")
        return state

    def _timeline_by_id(self, project, timeline_id: str):
        count = _integer(_call(project, "GetTimelineCount"), "GetTimelineCount")
        for index in range(1, count + 1):
            timeline = _call(project, "GetTimelineByIndex", index)
            if _id(timeline) == timeline_id:
                return timeline
        raise NoActiveTimelineError("The requested timeline ID is not present in the current project.")

    def list_timelines(self) -> tuple[dict, ...]:
        """Only names and stable IDs; inactive timeline track flags are unreliable."""
        project = self._project()
        project_id = _id(project)
        count = _integer(_call(project, "GetTimelineCount"), "GetTimelineCount")
        result = []
        for index in range(1, count + 1):
            timeline = _call(project, "GetTimelineByIndex", index)
            result.append({"id": _id(timeline), "name": _text(_call(timeline, "GetName"), "Timeline.GetName")})
        if _id(self._project()) != project_id:
            raise ContextChangedError("Project changed while listing timelines.")
        return tuple(result)

    def read_timeline(self, timeline_id: str | None = None) -> ResolveState:
        connection = ConnectionInfo(
            _text(_call(self._resolve, "GetVersionString"), "GetVersionString"),
            _text(_call(self._resolve, "GetProductName"), "GetProductName"),
            self._module_path, self._loading_method,
        )
        project, timeline = self._context()
        if timeline_id is not None and _id(timeline) != timeline_id:
            raise NoActiveTimelineError("Select the requested timeline explicitly before reading its edit state; inactive timeline track flags are unreliable in Resolve 21.")
        project_state = ProjectState(_id(project), _text(_call(project, "GetName"), "Project.GetName"))
        timeline_id = _id(timeline)
        # Query the active timeline, which may override the project frame rate.
        rate = parse_frame_rate(_call(timeline, "GetSetting", "timelineFrameRate"))
        start = _integer(_call(timeline, "GetStartFrame"), "Timeline.GetStartFrame")
        end = _integer(_call(timeline, "GetEndFrame"), "Timeline.GetEndFrame")
        tracks = tuple(self._read_tracks(timeline, "video") + self._read_tracks(timeline, "audio"))
        state = TimelineState(
            timeline_id, _text(_call(timeline, "GetName"), "Timeline.GetName"),
            rate.value, rate, start, end,
            _text(_call(timeline, "GetStartTimecode"), "GetStartTimecode"), tracks,
            self._read_markers(timeline),
        )
        # Detect a project/timeline switch during enumeration. This is not an
        # atomic snapshot and does not claim to detect edits within that timeline.
        try:
            current_project, current_timeline = self._context()
        except (NoProjectError, NoActiveTimelineError) as exc:
            raise ContextChangedError("The active context changed during the read. Retry when it is stable.") from exc
        if (_id(current_project), _id(current_timeline)) != (project_state.id, timeline_id):
            raise ContextChangedError("The active project or timeline changed during the read. Retry when it is stable.")
        return ResolveState(2, connection, project_state, state)

    def _read_markers(self, timeline) -> tuple[MarkerState, ...]:
        markers = _call(timeline, "GetMarkers")
        if not isinstance(markers, dict):
            raise UnexpectedAPIResponseError("GetMarkers must return a dictionary.")
        result = []
        for frame, marker in markers.items():
            if not isinstance(marker, dict):
                raise UnexpectedAPIResponseError("Marker information must be a dictionary.")
            result.append(MarkerState(
                _integer(frame, "marker frame"),
                _text(marker.get("color"), "marker color"),
                _text(marker.get("name"), "marker name", allow_empty=True),
                _text(marker.get("note", ""), "marker note", allow_empty=True),
                _integer(marker.get("duration"), "marker duration"),
                _text(marker.get("customData", ""), "marker customData", allow_empty=True),
            ))
        return tuple(sorted(result, key=lambda marker: marker.offset_frame))

    def _read_tracks(self, timeline: Any, track_type: TrackType) -> list[TrackState]:
        count = _integer(_call(timeline, "GetTrackCount", track_type), "GetTrackCount")
        if count < 0:
            raise UnexpectedAPIResponseError("GetTrackCount returned a negative count.")
        tracks = []
        for index in range(1, count + 1):
            name = _text(_call(timeline, "GetTrackName", track_type, index), "GetTrackName", allow_empty=True)
            native_items = _call(timeline, "GetItemListInTrack", track_type, index)
            if not isinstance(native_items, (list, tuple)):
                raise UnexpectedAPIResponseError("GetItemListInTrack must return a list; an API failure is not an empty track.")
            items = tuple(self._read_item(item, track_type, index) for item in native_items)
            locked = _boolean(_call(timeline, "GetIsTrackLocked", track_type, index), "GetIsTrackLocked")
            enabled = _boolean(_call(timeline, "GetIsTrackEnabled", track_type, index), "GetIsTrackEnabled")
            tracks.append(TrackState(track_type, index, name, items, locked, enabled))
        return tracks

    def _read_item(self, item: Any, track_type: TrackType, index: int) -> TimelineItemState:
        # Resolve 21 documents this explicit precision argument. Never round
        # an unexpected fractional response silently.
        start = _integer(_call(item, "GetStart", False), "TimelineItem.GetStart")
        end = _integer(_call(item, "GetEnd", False), "TimelineItem.GetEnd")
        duration = _integer(_call(item, "GetDuration", False), "TimelineItem.GetDuration")
        if end < start or duration < 0:
            raise UnexpectedAPIResponseError("Timeline item has an invalid frame range or duration.")
        media = _call(item, "GetMediaPoolItem")
        source_start = _call(item, "GetSourceStartFrame") if media is not None else None
        source_end = _call(item, "GetSourceEndFrame") if media is not None else None
        return TimelineItemState(
            _id(item), _text(_call(item, "GetName"), "TimelineItem.GetName", allow_empty=True),
            _id(media) if media is not None else None,
            track_type, index, start, end, duration,
            _integer(source_start, "GetSourceStartFrame") if source_start is not None else None,
            _integer(source_end, "GetSourceEndFrame") if source_end is not None else None,
        )
