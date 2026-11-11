from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resolve_bridge.timeline import ResolveAdapter


def api(**responses):
    """Only explicitly listed API methods exist; calls are recorded for assertions."""
    return SimpleNamespace(**{name: Mock(return_value=value) for name, value in responses.items()})


@pytest.fixture
def scene():
    media = api(GetUniqueId="media-1")
    item = api(
        GetUniqueId="clip-1", GetName="Interview_A.mov", GetMediaPoolItem=media,
        GetStart=86400, GetEnd=86640, GetDuration=240,
        GetSourceStartFrame=0, GetSourceEndFrame=239,
    )
    timeline = api(
        GetUniqueId="timeline-1", GetName="TEST_TIMELINE",
        GetSetting="24", GetStartFrame=86400, GetEndFrame=86640,
        GetStartTimecode="01:00:00:00", GetTrackCount=1,
        GetTrackName="", GetItemListInTrack=[],
        GetMarkers={}, GetIsTrackLocked=False, GetIsTrackEnabled=True,
    )
    timeline.GetTrackName.side_effect = lambda kind, index: "V1" if kind == "video" else "A1"
    timeline.GetItemListInTrack.side_effect = lambda kind, index: [item] if kind == "video" else []
    project = api(GetUniqueId="project-1", GetName="AI Bridge Test", GetCurrentTimeline=timeline)
    project.GetTimelineCount = Mock(return_value=1)
    project.GetTimelineByIndex = Mock(return_value=timeline)
    manager = api(GetCurrentProject=project)
    resolve = api(GetVersionString="21.0.0.48", GetProductName="DaVinci Resolve Studio", GetProjectManager=manager)
    adapter = ResolveAdapter(resolve, "/fake/fusionscript.so", "test")
    return SimpleNamespace(**locals())
