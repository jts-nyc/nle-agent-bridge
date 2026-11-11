from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resolve_bridge.errors import InvalidOperationError, OperationUncertainError, ProjectScopeError, ResolveWriteError, StaleSnapshotError, VerificationError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.operations import EditingService


def api(**values):
    return SimpleNamespace(**{name: Mock(return_value=value) for name, value in values.items()})


@pytest.fixture
def editing(scene, tmp_path):
    scene.media.GetName = Mock(return_value="Interview_A.mov")
    scene.media.GetClipProperty = Mock(return_value={"File Name": "Interview_A.mov", "File Path": "/fake/Interview_A.mov", "Frames": "1000", "FPS": "24", "Type": "Video + Audio", "Start": "0", "Online Status": "Online"})
    folder = api(GetUniqueId="root", GetName="Master", GetClipList=[scene.media], GetSubFolderList=[])
    pool = api(GetRootFolder=folder)
    scene.project.GetMediaPool = Mock(return_value=pool)
    tracks = {"video": [scene.item], "audio": []}
    scene.timeline.GetItemListInTrack.side_effect = lambda kind, index: tracks[kind]

    def append(clips):
        clip = clips[0]
        duration = clip["endFrame"] - clip["startFrame"]
        item = api(GetUniqueId="new-item", GetName="Interview_A.mov", GetMediaPoolItem=scene.media,
                   GetStart=clip["recordFrame"], GetEnd=clip["recordFrame"] + duration, GetDuration=duration,
                   GetSourceStartFrame=clip["startFrame"], GetSourceEndFrame=min(clip["endFrame"], 999))
        tracks["video" if clip["mediaType"] == 1 else "audio"].append(item)
        scene.timeline.GetEndFrame.return_value = max(scene.timeline.GetEndFrame.return_value, clip["recordFrame"] + duration)
        return [item]

    def marker(frame, color, name, note, duration, custom_data):
        scene.timeline.GetMarkers.return_value[frame] = {"color": color, "name": name, "note": note, "duration": duration, "customData": custom_data}
        return True

    pool.AppendToTimeline = Mock(side_effect=append)
    scene.timeline.AddMarker = Mock(side_effect=marker)
    service = EditingService(scene.adapter, "project-1", "timeline-1", OperationJournal(tmp_path / "j.db"))
    return SimpleNamespace(scene=scene, service=service, pool=pool, tracks=tracks)


def plan(editing, **overrides):
    arguments = {"media_id": "media-1", "source_in_frame": 72, "source_out_frame": 168,
                 "track_type": "video", "track_index": 1, "record_frame": 86640,
                 "expected_snapshot_id": editing.service.read().snapshot_id}
    return editing.service.plan_append_clip(**(arguments | overrides))


def test_plan_is_read_only_and_execution_is_verified_and_idempotent(editing):
    proposed = plan(editing)
    editing.pool.AppendToTimeline.assert_not_called()
    first = editing.service.execute_append_clip(proposed, idempotency_key="append")
    assert first["status"] == "verified"
    assert first["result"]["item"]["start_frame"] == 86640
    assert first["result"]["item"]["source_end_frame"] == 168
    assert editing.pool.AppendToTimeline.call_args.args[0][0]["endFrame"] == 168
    repeated = editing.service.execute_append_clip(proposed, idempotency_key="append")
    assert repeated["replayed"] is True
    editing.pool.AppendToTimeline.assert_called_once()


@pytest.mark.parametrize("overrides", [
    {"source_in_frame": -1}, {"source_in_frame": True}, {"source_out_frame": 72},
    {"source_out_frame": 1001}, {"track_index": 0}, {"track_index": 2},
    {"track_type": "subtitle"}, {"record_frame": 86399}, {"record_frame": 86410},
])
def test_invalid_append_never_mutates(editing, overrides):
    with pytest.raises(InvalidOperationError):
        plan(editing, **overrides)
    editing.pool.AppendToTimeline.assert_not_called()


def test_stale_plan_never_mutates(editing):
    proposed = plan(editing)
    editing.scene.item.GetSourceStartFrame.return_value = 1
    with pytest.raises(StaleSnapshotError):
        editing.service.execute_append_clip(proposed, idempotency_key="append")
    editing.pool.AppendToTimeline.assert_not_called()


def test_changed_media_since_plan_never_mutates(editing):
    proposed = plan(editing)
    editing.scene.media.GetClipProperty.return_value["File Path"] = "/different.mov"
    with pytest.raises(InvalidOperationError, match="changed since planning"):
        editing.service.execute_append_clip(proposed, idempotency_key="append")
    editing.pool.AppendToTimeline.assert_not_called()


def test_mixed_rate_is_rejected(editing):
    editing.scene.media.GetClipProperty.return_value["FPS"] = "29.97"
    with pytest.raises(InvalidOperationError, match="Mixed"):
        plan(editing)


def test_locked_track_is_rejected(editing):
    editing.scene.timeline.GetIsTrackLocked.return_value = True
    with pytest.raises(InvalidOperationError, match="locked"):
        plan(editing)


def test_false_success_is_detected_and_not_retried(editing):
    proposed = plan(editing)
    editing.pool.AppendToTimeline.side_effect = None
    editing.pool.AppendToTimeline.return_value = [editing.scene.item]
    with pytest.raises(VerificationError):
        editing.service.execute_append_clip(proposed, idempotency_key="append")
    with pytest.raises(OperationUncertainError):
        editing.service.execute_append_clip(proposed, idempotency_key="append")
    editing.pool.AppendToTimeline.assert_called_once()


def test_wrong_source_range_detected_after_write(editing):
    proposed = plan(editing)
    original = editing.pool.AppendToTimeline.side_effect

    def wrong_source(clips):
        items = original(clips)
        items[0].GetSourceStartFrame.return_value = 0
        return items

    editing.pool.AppendToTimeline.side_effect = wrong_source
    with pytest.raises(VerificationError, match="source/destination"):
        editing.service.execute_append_clip(proposed, idempotency_key="append")


def test_marker_converts_absolute_frame_to_offset_and_verifies(editing):
    args = {"frame": 86520, "color": "Blue", "name": "AI TEST", "note": "",
            "expected_snapshot_id": editing.service.read().snapshot_id, "idempotency_key": "marker"}
    result = editing.service.add_marker(**args)
    assert result["result"]["marker"]["offset_frame"] == 120
    assert editing.scene.timeline.AddMarker.call_args.args[0] == 120
    assert editing.service.add_marker(**args)["replayed"] is True
    editing.scene.timeline.AddMarker.assert_called_once()


def test_lying_marker_return_is_detected(editing):
    editing.scene.timeline.AddMarker.side_effect = None
    editing.scene.timeline.AddMarker.return_value = True
    with pytest.raises(VerificationError):
        editing.service.add_marker(frame=86520, color="Blue", name="TEST",
            expected_snapshot_id=editing.service.read().snapshot_id, idempotency_key="marker")


def test_wrong_project_never_mutates(editing):
    proposed = plan(editing)
    editing.scene.project.GetUniqueId.return_value = "other-project"
    with pytest.raises(ProjectScopeError):
        editing.service.execute_append_clip(proposed, idempotency_key="append")
    editing.pool.AppendToTimeline.assert_not_called()


def test_marker_collision_preserves_existing_marker(editing):
    editing.scene.timeline.GetMarkers.return_value = {120: {"name": "user", "color": "Red", "duration": 1}}
    with pytest.raises(InvalidOperationError, match="already occupies"):
        editing.service.add_marker(frame=86520, color="Blue", name="TEST",
            expected_snapshot_id=editing.service.read().snapshot_id, idempotency_key="marker")
    editing.scene.timeline.AddMarker.assert_not_called()


def test_source_end_at_eof_is_clamped_by_resolve_accessor(editing):
    proposed = plan(editing, source_in_frame=990, source_out_frame=1000)
    result = editing.service.execute_append_clip(proposed, idempotency_key="eof")
    assert result["result"]["item"]["duration_frames"] == 10
    assert result["result"]["item"]["source_end_frame"] == 999


@pytest.mark.parametrize("key,value", [("Online Status", "Offline"), ("Online Status", ""), ("Start", "1001")])
def test_offline_unknown_status_or_source_origin_is_rejected(editing, key, value):
    editing.scene.media.GetClipProperty.return_value[key] = value
    with pytest.raises(InvalidOperationError):
        plan(editing)
    editing.pool.AppendToTimeline.assert_not_called()


def test_uncalibrated_resolve_build_is_rejected(editing):
    editing.scene.resolve.GetVersionString.return_value = "22.0.0.1"
    with pytest.raises(InvalidOperationError, match="calibrated"):
        plan(editing)


@pytest.mark.parametrize("change_existing", [False, True])
def test_track_creation_preserves_existing_state(editing, change_existing):
    scene = editing.scene
    count = [1]
    scene.timeline.GetTrackCount.side_effect = lambda kind: count[0] if kind == "video" else 1
    scene.timeline.GetItemListInTrack.side_effect = lambda kind, index: editing.tracks[kind] if index == 1 else []

    def add_track(kind):
        assert kind == "video"
        count[0] += 1
        if change_existing:
            scene.item.GetName.return_value = "unexpectedly changed"
        return True

    scene.timeline.AddTrack = Mock(side_effect=add_track)
    if change_existing:
        with pytest.raises(VerificationError, match="Other timeline state"):
            editing.service.ensure_video_track(2, idempotency_key="track")
    else:
        assert editing.service.ensure_video_track(2, idempotency_key="track")["result"]["video_tracks"] == 2
    scene.timeline.AddTrack.assert_called_once()


def test_native_write_exception_is_typed_and_blocks_replay(editing):
    proposed = plan(editing)
    editing.pool.AppendToTimeline.side_effect = RuntimeError("connection lost during append")
    with pytest.raises(ResolveWriteError, match="partly applied"):
        editing.service.execute_append_clip(proposed, idempotency_key="append")
    with pytest.raises(OperationUncertainError):
        editing.service.execute_append_clip(proposed, idempotency_key="append")
    editing.pool.AppendToTimeline.assert_called_once()
