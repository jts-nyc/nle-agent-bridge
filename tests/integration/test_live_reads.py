"""Opt-in tests: reads only; live mutation tests are in scripts/smoke_test.py."""

import json
import os

import pytest

from resolve_bridge.connection import connect
from resolve_bridge.media import find_media

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live():
    if os.environ.get("RESOLVE_BRIDGE_LIVE") != "1":
        pytest.skip("Set RESOLVE_BRIDGE_LIVE=1 and RESOLVE_BRIDGE_PROJECT_ID to opt in.")
    project_id = os.environ.get("RESOLVE_BRIDGE_PROJECT_ID")
    if not project_id:
        pytest.fail("Live tests require an explicitly authorized stable project ID.")
    adapter = connect()
    state = adapter.read_timeline()
    assert state.project.id == project_id, "Open project is outside the live-test scope."
    return adapter, project_id


def test_live_timeline_json_and_stable_snapshot(live):
    adapter, project_id = live
    first, second = adapter.read_timeline(), adapter.read_timeline()
    assert first.project.id == second.project.id == project_id
    assert first.snapshot_id == second.snapshot_id, "Timeline changed between reads."
    assert json.loads(json.dumps(first.to_dict()))["timeline"]["id"] == first.timeline.id


def test_live_media_search_returns_every_exact_name_match(live):
    adapter, project_id = live
    inventory = adapter.read_media()
    assert inventory.project.id == project_id
    sources = [item for item in inventory.items if item.media_type != "Timeline"]
    assert sources, "The disposable test project needs source media."
    for item in sources:
        matches = find_media(inventory, item.name, exact=True)
        assert item.id in {match.id for match in matches}


def test_live_source_ids_match_inventory(live):
    adapter, project_id = live
    state = adapter.read_timeline()
    inventory = adapter.read_media()
    assert state.project.id == inventory.project.id == project_id
    media_ids = {item.id for item in inventory.items}
    for track in state.timeline.tracks:
        for item in track.items:
            assert item.media_id is None or item.media_id in media_ids
