"""Pure read-after-write checks against normalized state."""

from dataclasses import replace

from .errors import VerificationError


def verify_append(before, after, plan):
    old = {item.id: item for track in before.timeline.tracks for item in track.items}
    current = {item.id: item for track in after.timeline.tracks for item in track.items}
    inserted = [item for key, item in current.items() if key not in old]
    if len(inserted) != 1 or any(current.get(key) != item for key, item in old.items()):
        raise VerificationError("Expected exactly one new item and all existing items unchanged.")
    item = inserted[0]
    expected = (plan.media_id, plan.track_type, plan.track_index, plan.record_frame,
                plan.record_frame + plan.source_out_frame - plan.source_in_frame,
                plan.source_out_frame - plan.source_in_frame,
                plan.source_in_frame, min(plan.source_out_frame, plan.media_duration_frames - 1))
    observed = (item.media_id, item.track_type, item.track_index, item.start_frame,
                item.end_frame, item.duration_frames, item.source_start_frame, item.source_end_frame)
    if observed != expected:
        raise VerificationError(f"Inserted item differs from the requested source/destination: {observed!r}.")
    remaining_tracks = tuple(replace(track, items=tuple(clip for clip in track.items if clip.id != item.id)) for track in after.timeline.tracks)
    without_insert = replace(after, timeline=replace(after.timeline, tracks=remaining_tracks, end_frame=before.timeline.end_frame))
    if without_insert.snapshot_id != before.snapshot_id:
        raise VerificationError("Other timeline state changed during insertion.")
    if after.timeline.end_frame != max(before.timeline.end_frame, item.end_frame):
        raise VerificationError("Unexpected timeline end after insertion.")
    return item


def verify_marker(before, after, expected):
    if expected not in after.timeline.markers:
        raise VerificationError("Requested marker was not found exactly on reread.")
    remaining = tuple(marker for marker in after.timeline.markers if marker != expected)
    restored = replace(after, timeline=replace(after.timeline, markers=remaining))
    if restored.snapshot_id != before.snapshot_id:
        raise VerificationError("State other than the requested marker changed.")
