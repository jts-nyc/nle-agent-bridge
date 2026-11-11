import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from resolve_bridge.errors import (
    ContextChangedError, NoActiveTimelineError, NoProjectError,
    ResolveReadError, UnexpectedAPIResponseError,
)


def test_normalized_read_preserves_offsets_and_empty_tracks(scene):
    data = json.loads(json.dumps(scene.adapter.read_timeline().to_dict()))
    assert data["project"] == {"id": "project-1", "name": "AI Bridge Test"}
    timeline = data["timeline"]
    assert timeline["start_frame"] == 86400
    assert timeline["start_timecode"] == "01:00:00:00"
    assert timeline["fps"] == 24
    assert timeline["tracks"][1]["items"] == []
    assert timeline["tracks"][0]["items"][0] == {
        "id": "clip-1", "name": "Interview_A.mov", "media_id": "media-1",
        "track_type": "video", "track_index": 1,
        "start_frame": 86400, "end_frame": 86640, "duration_frames": 240,
        "source_start_frame": 0, "source_end_frame": 239,
    }
    assert scene.timeline.GetTrackCount.call_args_list == [call("video"), call("audio")]
    assert scene.timeline.GetItemListInTrack.call_args_list == [call("video", 1), call("audio", 1)]
    for method in (scene.item.GetStart, scene.item.GetEnd, scene.item.GetDuration):
        method.assert_called_once_with(False)


def test_active_timeline_frame_rate_is_used(scene):
    scene.timeline.GetSetting.return_value = "29.97 DF"
    state = scene.adapter.read_timeline()
    assert state.timeline.frame_rate.numerator == 30000
    assert state.timeline.frame_rate.denominator == 1001
    assert state.timeline.frame_rate.drop_frame is True
    scene.timeline.GetSetting.assert_called_once_with("timelineFrameRate")


def test_generated_item_without_media_pool_item(scene):
    scene.item.GetMediaPoolItem.return_value = None
    assert scene.adapter.read_timeline().timeline.tracks[0].items[0].media_id is None


def test_duplicate_names_keep_distinct_resolve_ids(scene):
    second = SimpleNamespace(**vars(scene.item))
    second.GetUniqueId = Mock(return_value="clip-2")
    scene.timeline.GetItemListInTrack.side_effect = lambda kind, index: [scene.item, second] if kind == "video" else []
    items = scene.adapter.read_timeline().timeline.tracks[0].items
    assert items[0].name == items[1].name
    assert [item.id for item in items] == ["clip-1", "clip-2"]


@pytest.mark.parametrize("target,method,error", [
    ("manager", "GetCurrentProject", NoProjectError),
    ("project", "GetCurrentTimeline", NoActiveTimelineError),
    ("resolve", "GetProjectManager", UnexpectedAPIResponseError),
])
def test_missing_context(scene, target, method, error):
    getattr(getattr(scene, target), method).return_value = None
    with pytest.raises(error):
        scene.adapter.read_timeline()


@pytest.mark.parametrize("value", [None, False, -1, 1.5, "1", float("nan")])
def test_bad_track_counts(scene, value):
    scene.timeline.GetTrackCount.return_value = value
    with pytest.raises(UnexpectedAPIResponseError):
        scene.adapter.read_timeline()


@pytest.mark.parametrize("value", [None, {}, False, "clips"])
def test_bad_item_list_is_not_an_empty_track(scene, value):
    scene.timeline.GetItemListInTrack.side_effect = None
    scene.timeline.GetItemListInTrack.return_value = value
    with pytest.raises(UnexpectedAPIResponseError):
        scene.adapter.read_timeline()


@pytest.mark.parametrize("value", [True, None, "86400", 86400.5, float("inf")])
def test_bad_frame_value_is_never_silently_rounded(scene, value):
    scene.item.GetStart.return_value = value
    with pytest.raises(UnexpectedAPIResponseError):
        scene.adapter.read_timeline()


def test_integral_float_response_is_normalized(scene):
    scene.item.GetStart.return_value = 86400.0
    assert type(scene.adapter.read_timeline().timeline.tracks[0].items[0].start_frame) is int


def test_bad_id_is_not_replaced_with_clip_name(scene):
    scene.item.GetUniqueId.return_value = ""
    with pytest.raises(UnexpectedAPIResponseError):
        scene.adapter.read_timeline()


def test_missing_method_is_typed(scene):
    del scene.item.GetDuration
    with pytest.raises(UnexpectedAPIResponseError, match="GetDuration"):
        scene.adapter.read_timeline()


def test_disconnect_during_read_is_typed(scene):
    scene.timeline.GetTrackCount.side_effect = RuntimeError("native connection gone")
    with pytest.raises(ResolveReadError, match="GetTrackCount"):
        scene.adapter.read_timeline()


@pytest.mark.parametrize("target", ["project", "timeline"])
def test_context_switch_is_detected(scene, target):
    getattr(scene, target).GetUniqueId.side_effect = ["original", "switched"]
    with pytest.raises(ContextChangedError):
        scene.adapter.read_timeline()


def test_project_closed_during_read(scene):
    scene.manager.GetCurrentProject.side_effect = [scene.project, None]
    with pytest.raises(ContextChangedError):
        scene.adapter.read_timeline()


def test_no_tracks_is_valid(scene):
    scene.timeline.GetTrackCount.return_value = 0
    assert scene.adapter.read_timeline().timeline.tracks == ()
    scene.timeline.GetItemListInTrack.assert_not_called()


def test_inactive_timeline_snapshot_is_rejected_without_selection(scene):
    with pytest.raises(NoActiveTimelineError, match="inactive timeline"):
        scene.adapter.read_timeline("different-timeline-id")


def test_timeline_listing_only_returns_stable_ids_and_names(scene):
    assert scene.adapter.list_timelines() == ({"id": "timeline-1", "name": "TEST_TIMELINE"},)
