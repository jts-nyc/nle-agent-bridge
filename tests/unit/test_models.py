from dataclasses import FrozenInstanceError
import json

import pytest


def test_state_is_immutable_and_json_serializable(scene):
    state = scene.adapter.read_timeline()
    with pytest.raises(FrozenInstanceError):
        state.timeline.name = "new name"
    data = json.loads(json.dumps(state.to_dict(), allow_nan=False))
    assert data["schema_version"] == 2
    assert data["snapshot_id"].startswith("timeline-v2-")
    assert data["connection"]["loading_method"] == "test"
