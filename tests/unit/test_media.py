from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resolve_bridge.errors import AmbiguousMediaError, ContextChangedError, InvalidOperationError, MediaNotFoundError, UnexpectedAPIResponseError
from resolve_bridge.media import find_media, require_unique_media


def api(**values):
    return SimpleNamespace(**{key: Mock(return_value=value) for key, value in values.items()})


@pytest.fixture
def media_scene(scene):
    clip = api(GetUniqueId="m-1", GetName="Campus_Broll.mov", GetClipProperty={
        "File Name": "Campus_Broll.mov", "File Path": "/media/Campus_Broll.mov",
        "Frames": "384", "FPS": "24", "Type": "Video + Audio",
    })
    child = api(GetUniqueId="bin-2", GetName="B-Roll", GetClipList=[clip], GetSubFolderList=[])
    root = api(GetUniqueId="bin-1", GetName="Master", GetClipList=[], GetSubFolderList=[child])
    scene.project.GetMediaPool = Mock(return_value=api(GetRootFolder=root))
    scene.clip, scene.child, scene.root = clip, child, root
    return scene


def test_nested_inventory_with_no_active_timeline(media_scene):
    media_scene.project.GetCurrentTimeline.return_value = None
    inventory = media_scene.adapter.read_media()
    assert [b.path for b in inventory.bins] == [("Master",), ("Master", "B-Roll")]
    assert inventory.items[0].duration_frames == 384
    assert inventory.items[0].source_path == "/media/Campus_Broll.mov"
    assert inventory.items[0].bin_id == "bin-2"
    assert inventory.items[0].frame_rate.numerator == 24
    media_scene.project.GetCurrentTimeline.assert_not_called()


def test_search_and_explicit_ambiguity(media_scene):
    inventory = media_scene.adapter.read_media()
    item = inventory.items[0]
    assert require_unique_media(inventory, "CAMPUS").id == "m-1"
    assert find_media(inventory, "campus", exact=True) == ()
    assert require_unique_media(inventory, "campus_broll.MOV", exact=True).id == "m-1"
    duplicate = replace(item, id="m-2", bin_id="another-bin")
    ambiguous = replace(inventory, items=(item, duplicate))
    assert len(find_media(ambiguous, "campus")) == 2
    with pytest.raises(AmbiguousMediaError, match="m-2"):
        require_unique_media(ambiguous, "campus")
    with pytest.raises(MediaNotFoundError):
        require_unique_media(inventory, "missing")


def test_unicode_and_timeline_exclusion(media_scene):
    inventory = media_scene.adapter.read_media()
    item = replace(inventory.items[0], name="Café", file_name=None)
    inventory = replace(inventory, items=(item, replace(item, id="timeline", media_type="Timeline")))
    assert find_media(inventory, "CAFE\u0301") == (item,)


@pytest.mark.parametrize("query", [None, "", "   ", 12])
def test_invalid_query(media_scene, query):
    with pytest.raises(InvalidOperationError):
        find_media(media_scene.adapter.read_media(), query)


def test_missing_metadata_is_explicit(media_scene):
    media_scene.clip.GetClipProperty.return_value = {}
    item = media_scene.adapter.read_media().items[0]
    assert item.duration_frames is None
    assert item.frame_rate is None
    assert item.source_path is None


def test_unrecognized_metadata_warns(media_scene):
    media_scene.clip.GetClipProperty.return_value = {"Frames": "unknown", "FPS": "VFR"}
    item = media_scene.adapter.read_media().items[0]
    assert len(item.warnings) == 2


@pytest.mark.parametrize("field", ["GetClipList", "GetSubFolderList"])
def test_bad_list_is_not_silently_empty(media_scene, field):
    getattr(media_scene.root, field).return_value = None
    with pytest.raises(UnexpectedAPIResponseError):
        media_scene.adapter.read_media()


def test_folder_cycle_fails(media_scene):
    media_scene.child.GetSubFolderList.return_value = [media_scene.root]
    with pytest.raises(UnexpectedAPIResponseError, match="cycle"):
        media_scene.adapter.read_media()


def test_project_switch_fails(media_scene):
    media_scene.project.GetUniqueId.side_effect = ["before", "after"]
    with pytest.raises(ContextChangedError):
        media_scene.adapter.read_media()


def test_same_bin_names_have_distinct_ids(media_scene):
    other = api(GetUniqueId="bin-3", GetName="B-Roll", GetClipList=[], GetSubFolderList=[])
    media_scene.root.GetSubFolderList.return_value.append(other)
    bins = media_scene.adapter.read_media().bins
    assert bins[1].path == bins[2].path
    assert bins[1].id != bins[2].id
