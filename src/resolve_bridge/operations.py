"""Narrow planned edits, bound to a project/timeline and durable journal.

Source ranges are half-open [in, out) in this interface and in the calibrated
Resolve 21.0.0.48 append call. The installed example's inclusive convention was
contradicted by eight live probes. Uncalibrated builds and mixed rates fail closed.
"""

from dataclasses import asdict, dataclass, replace
from fractions import Fraction
import hashlib
import json
import uuid

from . import calibration
from ._api import _call, _id, _integer, _list, _write
from .errors import (InvalidOperationError, MediaNotFoundError, ProjectScopeError, UncalibratedBuildError,
                     VerificationError)
from .models import MarkerState
from .snapshot import require_snapshot
from .verification import verify_append, verify_marker


@dataclass(frozen=True)
class AppendClipPlan:
    plan_id: str
    project_id: str
    timeline_id: str
    expected_snapshot_id: str
    media_id: str
    media_fingerprint: str
    media_duration_frames: int
    source_in_frame: int
    source_out_frame: int
    track_type: str
    track_index: int
    record_frame: int

    def to_dict(self):
        return asdict(self)


def _media_fingerprint(media):
    return hashlib.sha256(json.dumps(asdict(media), sort_keys=True, allow_nan=False).encode()).hexdigest()


def _frame(value, name):
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise InvalidOperationError(f"{name} must be an integer between 0 and 2147483647.")
    return value


class EditingService:
    def __init__(self, adapter, project_id: str, timeline_id: str, journal):
        if not all(isinstance(value, str) and value for value in (project_id, timeline_id)):
            raise InvalidOperationError("Editing requires explicit project and timeline IDs.")
        self._adapter = adapter
        self.project_id, self.timeline_id, self.journal = project_id, timeline_id, journal

    def _guard(self):
        project = self._adapter._project()
        if _id(project) != self.project_id:
            raise ProjectScopeError("Active project is outside the authorized editing scope.")
        return project, self._adapter._timeline_by_id(project, self.timeline_id)

    def read(self):
        self._guard()
        state = self._adapter.read_timeline(self.timeline_id)
        if state.project.id != self.project_id:
            raise ProjectScopeError("Project changed during the read.")
        return state

    def _media(self, media_id):
        inventory = self._adapter.read_media()
        if inventory.project.id != self.project_id:
            raise ProjectScopeError("Project changed during media lookup.")
        matches = [item for item in inventory.items if item.id == media_id]
        if len(matches) != 1:
            raise MediaNotFoundError("The requested stable media ID is not present exactly once.")
        return matches[0]

    def _validate_append(self, state, media, source_in, source_out, track_type, track_index, record):
        if not calibration.is_calibrated(state.connection.resolve_version):
            raise UncalibratedBuildError(f"Append source-boundary semantics are only calibrated for Resolve {calibration.describe()}.")
        for value, name in ((source_in, "source_in_frame"), (source_out, "source_out_frame"), (record, "record_frame")):
            _frame(value, name)
        if source_out <= source_in or media.duration_frames is None or source_out > media.duration_frames:
            raise InvalidOperationError("Source range must be nonempty and within a known source frame count.")
        if media.source_start_frame != 0:
            raise InvalidOperationError("Only sources with a known zero-based source frame origin are supported.")
        if media.online_status != "Online":
            raise InvalidOperationError("Source media must report Online status before insertion.")
        if track_type not in ("video", "audio") or type(track_index) is not int or track_index < 1:
            raise InvalidOperationError("Track must be video/audio with a positive one-based integer index.")
        if media.media_type is None or track_type not in media.media_type.casefold():
            raise InvalidOperationError("Media does not report the requested video/audio stream type.")
        source_rate, timeline_rate = media.frame_rate, state.timeline.frame_rate
        if source_rate is None or Fraction(source_rate.numerator, source_rate.denominator) != Fraction(timeline_rate.numerator, timeline_rate.denominator):
            raise InvalidOperationError("Mixed or unknown source/timeline frame rates are not yet supported for insertion.")
        track = next((track for track in state.timeline.tracks if (track.type, track.index) == (track_type, track_index)), None)
        if track is None or track.locked or not track.enabled:
            raise InvalidOperationError("Target track is missing, locked, or disabled.")
        end = record + source_out - source_in
        _frame(end, "record_end_frame")
        if record < state.timeline.start_frame:
            raise InvalidOperationError("Record frame precedes the timeline start.")
        if any(record < item.end_frame and item.start_frame < end for item in track.items):
            raise InvalidOperationError("Insertion would overlap an existing item; choose an empty range.")

    def plan_append_clip(self, *, media_id: str, source_in_frame: int, source_out_frame: int,
                         track_type: str, track_index: int, record_frame: int, expected_snapshot_id: str) -> AppendClipPlan:
        state = self.read()
        require_snapshot(state, expected_snapshot_id)
        media = self._media(media_id)
        self._validate_append(state, media, source_in_frame, source_out_frame, track_type, track_index, record_frame)
        return AppendClipPlan(str(uuid.uuid4()), self.project_id, self.timeline_id, state.snapshot_id,
                              media_id, _media_fingerprint(media), media.duration_frames, source_in_frame, source_out_frame,
                              track_type, track_index, record_frame)

    def _native_media(self, project, media_id):
        # Private lookup immediately before writing; no native handle is returned
        # to the controller. Folder traversal is bounded by unique folder IDs.
        pool = _call(project, "GetMediaPool")
        pending, seen, matches = [_call(pool, "GetRootFolder")], set(), []
        while pending:
            folder = pending.pop()
            folder_id = _id(folder)
            if folder_id in seen:
                raise InvalidOperationError("Repeated Media Pool folder during native lookup.")
            seen.add(folder_id)
            matches.extend(item for item in _list(_call(folder, "GetClipList"), "GetClipList") if _id(item) == media_id)
            pending.extend(_list(_call(folder, "GetSubFolderList"), "GetSubFolderList"))
        if len(matches) != 1:
            raise MediaNotFoundError("Media disappeared or became ambiguous before execution.")
        return pool, matches[0]

    def execute_append_clip(self, plan: AppendClipPlan, *, idempotency_key: str) -> dict:
        self._guard()
        if not isinstance(plan, AppendClipPlan) or (plan.project_id, plan.timeline_id) != (self.project_id, self.timeline_id):
            raise ProjectScopeError("Plan belongs to a different project or timeline.")

        def action(operation_id):
            before = self.read()
            require_snapshot(before, plan.expected_snapshot_id)
            media = self._media(plan.media_id)
            if _media_fingerprint(media) != plan.media_fingerprint or media.duration_frames != plan.media_duration_frames:
                raise InvalidOperationError("Source media changed since planning.")
            self._validate_append(before, media, plan.source_in_frame, plan.source_out_frame, plan.track_type, plan.track_index, plan.record_frame)
            project, timeline = self._guard()
            pool, native_media = self._native_media(project, plan.media_id)
            self._guard()
            if _id(_call(project, "GetCurrentTimeline")) != self.timeline_id:
                raise VerificationError("Resolve did not select the intended insertion timeline.")
            # Recheck immediately before the write; execution never implicitly
            # switches a timeline selected by the user.
            require_snapshot(self.read(), plan.expected_snapshot_id)
            self._guard()
            _write(pool, "AppendToTimeline", [{
                "mediaPoolItem": native_media, "startFrame": plan.source_in_frame,
                "endFrame": plan.source_out_frame,
                "mediaType": 1 if plan.track_type == "video" else 2,
                "trackIndex": plan.track_index, "recordFrame": plan.record_frame,
            }])
            after = self.read()
            inserted = verify_append(before, after, plan)
            return {"snapshot_before": before.snapshot_id, "snapshot_after": after.snapshot_id, "item": asdict(inserted)}

        return self.journal.execute("append_clip", plan.to_dict(), idempotency_key, action)

    def add_marker(self, *, frame: int, color: str, name: str, note: str = "", expected_snapshot_id: str, idempotency_key: str) -> dict:
        self._guard()
        _frame(frame, "frame")
        if color not in ("Blue", "Green", "Red", "Yellow", "Cyan", "Purple", "Pink", "Orange", "White"):
            raise InvalidOperationError("Marker color is not in the supported color allowlist.")
        if not isinstance(name, str) or not name or len(name) > 256 or not isinstance(note, str) or len(note) > 4096:
            raise InvalidOperationError("Marker requires a name up to 256 characters and a note up to 4096 characters.")
        parameters = {"project_id": self.project_id, "timeline_id": self.timeline_id, "frame": frame, "color": color, "name": name, "note": note, "expected_snapshot_id": expected_snapshot_id}

        def action(operation_id):
            before = self.read()
            require_snapshot(before, expected_snapshot_id)
            offset = frame - before.timeline.start_frame
            if offset < 0 or frame >= before.timeline.end_frame:
                raise InvalidOperationError("Marker frame must fall within the timeline's current frame range.")
            if any(marker.offset_frame == offset for marker in before.timeline.markers):
                raise InvalidOperationError("A marker already occupies this frame.")
            expected = MarkerState(offset, color, name, note, 1, "resolve-bridge:" + operation_id)
            _, timeline = self._guard()
            _write(timeline, "AddMarker", offset, color, name, note, 1, expected.custom_data)
            after = self.read()
            verify_marker(before, after, expected)
            return {"snapshot_before": before.snapshot_id, "snapshot_after": after.snapshot_id, "marker": asdict(expected)}

        return self.journal.execute("add_marker", parameters, idempotency_key, action)

    def ensure_video_track(self, index: int, *, idempotency_key: str) -> dict:
        self._guard()
        if type(index) is not int or not 1 <= index <= 16:
            raise InvalidOperationError("Test video track index must be between 1 and 16.")

        def action(operation_id):
            before = self.read()
            count = len([track for track in before.timeline.tracks if track.type == "video"])
            original_count = count
            for _ in range(max(0, index - count)):
                _, timeline = self._guard()
                _write(timeline, "AddTrack", "video")
                new_count = _integer(_call(timeline, "GetTrackCount", "video"), "GetTrackCount")
                if new_count != count + 1:
                    raise VerificationError("Video track addition was not observed on reread.")
                count = new_count
            after = self.read()
            original_tracks = tuple(track for track in after.timeline.tracks if track.type != "video" or track.index <= original_count)
            restored = replace(after, timeline=replace(after.timeline, tracks=original_tracks))
            if restored.snapshot_id != before.snapshot_id:
                raise VerificationError("Other timeline state changed while adding video tracks.")
            return {"snapshot_before": before.snapshot_id, "snapshot_after": after.snapshot_id, "video_tracks": count}

        return self.journal.execute("ensure_video_track", {"project_id": self.project_id, "timeline_id": self.timeline_id, "index": index}, idempotency_key, action)
