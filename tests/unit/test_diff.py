from dataclasses import replace

import pytest

from resolve_bridge.diff import diff_states
from resolve_bridge.errors import InvalidOperationError
from resolve_bridge.models import MarkerState


def test_identical_reads_have_empty_diff(scene):
    state = scene.adapter.read_timeline()
    diff = diff_states(state, state)
    assert diff["identical"] is True and diff["timeline"] == {}
    assert diff["clips"] == diff["markers"] == diff["tracks"] == {"added": [], "removed": [], "changed": []}


def test_clip_marker_and_track_changes_are_itemized(scene):
    before = scene.adapter.read_timeline()
    track = before.timeline.tracks[0]
    moved = replace(track.items[0], start_frame=86410, end_frame=86650)
    after_tracks = (replace(track, items=(moved,), locked=True),) + before.timeline.tracks[1:]
    marker = MarkerState(10, "Blue", "Beat", "", 1, "")
    after = replace(before, timeline=replace(before.timeline, tracks=after_tracks, markers=(marker,), end_frame=86650))
    diff = diff_states(before, after)
    assert diff["identical"] is False
    assert diff["timeline"] == {"end_frame": [86640, 86650]}
    assert diff["clips"]["changed"] == [{"key": "clip-1", "fields": {"start_frame": [86400, 86410], "end_frame": [86640, 86650]}}]
    assert diff["markers"]["added"][0]["name"] == "Beat"
    assert diff["tracks"]["changed"] == [{"key": "video:1", "fields": {"locked": [False, True]}}]


def test_removed_clip_and_different_timeline(scene):
    before = scene.adapter.read_timeline()
    track = before.timeline.tracks[0]
    after = replace(before, timeline=replace(before.timeline, tracks=(replace(track, items=()),) + before.timeline.tracks[1:]))
    assert [c["id"] for c in diff_states(before, after)["clips"]["removed"]] == ["clip-1"]
    other = replace(before, timeline=replace(before.timeline, id="timeline-2"))
    with pytest.raises(InvalidOperationError):
        diff_states(before, other)
