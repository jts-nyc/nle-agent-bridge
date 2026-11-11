from dataclasses import replace

import pytest

from resolve_bridge.errors import StaleSnapshotError
from resolve_bridge.snapshot import require_snapshot


def test_unchanged_reads_have_same_snapshot(scene):
    first = scene.adapter.read_timeline()
    second = scene.adapter.read_timeline()
    assert first.snapshot_id == second.snapshot_id
    require_snapshot(second, first.snapshot_id)
    assert replace(first, connection=replace(first.connection, loading_method="different")).snapshot_id == first.snapshot_id


@pytest.mark.parametrize("change", ["source", "lock", "enabled", "marker", "project", "timeline", "fps"])
def test_edit_relevant_changes_invalidate_snapshot(scene, change):
    before = scene.adapter.read_timeline()
    if change == "source":
        scene.item.GetSourceStartFrame.return_value = 1
    elif change == "lock":
        scene.timeline.GetIsTrackLocked.return_value = True
    elif change == "enabled":
        scene.timeline.GetIsTrackEnabled.return_value = False
    elif change == "marker":
        scene.timeline.GetMarkers.return_value = {120.0: {"name": "TEST", "color": "Blue", "duration": 1.0}}
    elif change == "project":
        scene.project.GetUniqueId.return_value = "another-project"
    elif change == "timeline":
        scene.timeline.GetUniqueId.return_value = "another-timeline"
    elif change == "fps":
        scene.timeline.GetSetting.return_value = "25"
    after = scene.adapter.read_timeline()
    with pytest.raises(StaleSnapshotError):
        require_snapshot(after, before.snapshot_id)


def test_marker_dictionary_order_does_not_change_snapshot(scene):
    first = {120: {"name": "A", "color": "Blue", "duration": 1}, 240: {"name": "B", "color": "Red", "duration": 1}}
    scene.timeline.GetMarkers.return_value = first
    before = scene.adapter.read_timeline()
    scene.timeline.GetMarkers.return_value = dict(reversed(list(first.items())))
    assert scene.adapter.read_timeline().snapshot_id == before.snapshot_id
