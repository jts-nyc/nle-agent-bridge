from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from resolve_bridge.errors import InvalidOperationError, ProjectScopeError, VerificationError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.project import ProjectSetup, WORKSPACE_BINS


def api(**values):
    return SimpleNamespace(**{name: Mock(return_value=value) for name, value in values.items()})


@pytest.fixture
def setup_scene(scene, tmp_path):
    root = api(GetUniqueId="root", GetName="Master", GetClipList=[], GetSubFolderList=[])
    pool = api(GetRootFolder=root)
    scene.project.GetMediaPool = Mock(return_value=pool)
    counter = [0]

    def add_bin(parent, name):
        counter[0] += 1
        child = api(GetUniqueId=f"bin-{counter[0]}", GetName=name, GetClipList=[], GetSubFolderList=[])
        parent.GetSubFolderList.return_value.append(child)
        return child

    pool.AddSubFolder = Mock(side_effect=add_bin)
    timelines = [scene.timeline]
    scene.project.GetTimelineCount = Mock(side_effect=lambda: len(timelines))
    scene.project.GetTimelineByIndex = Mock(side_effect=lambda index: timelines[index - 1])

    def create_timeline(name):
        media = api(GetUniqueId="new-media", GetName=name, GetClipProperty={"Type": "Timeline"})
        timeline = api(GetUniqueId="new-timeline", GetName=name, GetMediaPoolItem=media)
        root.GetClipList.return_value.append(media)
        timelines.append(timeline)
        scene.project.GetCurrentTimeline.return_value = timeline
        return timeline

    def move_clips(clips, folder):
        for clip in clips:
            root.GetClipList.return_value.remove(clip)
            folder.GetClipList.return_value.append(clip)
        return True

    def set_current(timeline):
        scene.project.GetCurrentTimeline.return_value = timeline
        return True

    pool.CreateEmptyTimeline = Mock(side_effect=create_timeline)
    pool.MoveClips = Mock(side_effect=move_clips)
    scene.project.SetCurrentTimeline = Mock(side_effect=set_current)
    setup = ProjectSetup(scene.adapter, "project-1", OperationJournal(tmp_path / "j.db"))
    return SimpleNamespace(scene=scene, setup=setup, pool=pool, root=root)


def test_workspace_verified_and_idempotent(setup_scene):
    s = setup_scene
    result = s.setup.create_workspace(idempotency_key="workspace")
    assert result["status"] == "verified"
    assert set(result["result"]["bins"]) == {"AI Bridge", *WORKSPACE_BINS}
    assert s.pool.AddSubFolder.call_count == 7
    assert s.setup.create_workspace(idempotency_key="workspace")["replayed"] is True
    assert s.pool.AddSubFolder.call_count == 7


def test_wrong_project_cannot_write(setup_scene):
    s = setup_scene
    s.scene.project.GetUniqueId.return_value = "production-project"
    with pytest.raises(ProjectScopeError):
        s.setup.create_workspace(idempotency_key="workspace")
    s.pool.AddSubFolder.assert_not_called()


def test_project_switch_mid_setup_stops_further_writes(setup_scene):
    s = setup_scene
    original = s.pool.AddSubFolder.side_effect

    def switch_after_first(parent, name):
        result = original(parent, name)
        s.scene.project.GetUniqueId.return_value = "production-project"
        return result

    s.pool.AddSubFolder.side_effect = switch_after_first
    with pytest.raises(ProjectScopeError):
        s.setup.create_workspace(idempotency_key="workspace")
    assert s.pool.AddSubFolder.call_count == 1


def test_setup_verifies_bin_reread_not_just_api_return(setup_scene):
    s = setup_scene
    s.pool.AddSubFolder.side_effect = lambda *args: api(GetUniqueId="phantom")
    with pytest.raises(VerificationError):
        s.setup.create_workspace(idempotency_key="workspace")


def test_create_test_timeline_and_restore_original_selection(setup_scene):
    s = setup_scene
    result = s.setup.create_test_timeline("AI_BRIDGE_TEST_FIXTURE", idempotency_key="timeline")
    assert result["result"]["timeline_id"] == "new-timeline"
    assert s.scene.project.GetCurrentTimeline() is s.scene.timeline
    assert s.setup.create_test_timeline("AI_BRIDGE_TEST_FIXTURE", idempotency_key="timeline")["replayed"] is True
    s.pool.CreateEmptyTimeline.assert_called_once()


def test_select_timeline_verifies_and_records_previous(setup_scene):
    s = setup_scene
    s.setup.create_test_timeline("AI_BRIDGE_TEST_FIXTURE", idempotency_key="timeline")
    result = s.setup.select_timeline("new-timeline", idempotency_key="select")
    assert result["result"]["previous_timeline_id"] == "timeline-1"
    assert s.scene.project.GetCurrentTimeline().GetUniqueId() == "new-timeline"
    s.setup.select_timeline("timeline-1", idempotency_key="restore")
    assert s.scene.project.GetCurrentTimeline() is s.scene.timeline


def test_move_failure_is_detected(setup_scene):
    s = setup_scene
    s.pool.MoveClips.side_effect = None
    s.pool.MoveClips.return_value = True
    with pytest.raises(VerificationError):
        s.setup.create_test_timeline("AI_BRIDGE_TEST_FIXTURE", idempotency_key="timeline")


@pytest.mark.parametrize("name", ["Timeline 1", "AI_BRIDGE_TEST_/tmp", "", None])
def test_only_explicit_test_names_are_accepted(setup_scene, name):
    with pytest.raises(InvalidOperationError):
        setup_scene.setup.create_test_timeline(name, idempotency_key="timeline")
    setup_scene.pool.CreateEmptyTimeline.assert_not_called()


def test_organization_is_planned_and_verified_without_source_changes(setup_scene):
    s = setup_scene
    s.setup.create_workspace(idempotency_key="workspace")
    clip = api(GetUniqueId="footage", GetName="clip.mov", GetClipProperty={"Type": "Video + Audio", "File Path": "/original/clip.mov"})
    s.root.GetClipList.return_value.append(clip)
    proposed = s.setup.plan_organization()
    assert len(proposed["moves"]) == 1
    assert proposed["source_file_changes"] == []
    s.pool.MoveClips.assert_not_called()
    result = s.setup.execute_organization(proposed, idempotency_key="organize")
    assert result["result"]["source_paths_unchanged"] is True
    assert s.setup.execute_organization(proposed, idempotency_key="organize")["replayed"] is True
    s.pool.MoveClips.assert_called_once()


def test_modified_organization_plan_cannot_move_arbitrary_media(setup_scene):
    s = setup_scene
    s.setup.create_workspace(idempotency_key="workspace")
    proposed = s.setup.plan_organization()
    proposed["moves"].append({"media_id": "arbitrary", "to_bin_id": "arbitrary"})
    with pytest.raises(InvalidOperationError, match="changed"):
        s.setup.execute_organization(proposed, idempotency_key="organize")
    s.pool.MoveClips.assert_not_called()


def test_unknown_media_types_are_reported_and_not_guessed(setup_scene):
    s = setup_scene
    s.setup.create_workspace(idempotency_key="workspace")
    s.root.GetClipList.return_value.append(api(GetUniqueId="unknown", GetName="item", GetClipProperty={"Type": "Unknown"}))
    proposed = s.setup.plan_organization()
    assert proposed["moves"] == []
    assert proposed["unresolved"][0]["media_id"] == "unknown"


def test_backup_is_saved_and_archive_integrity_checked(setup_scene):
    s = setup_scene
    s.scene.manager.SaveProject = Mock(return_value=True)

    def export(name, path, with_stills):
        assert with_stills is False
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("project.xml", "<project/>")
        return True

    s.scene.manager.ExportProject = Mock(side_effect=export)
    result = s.setup.save_and_backup("test-backup", idempotency_key="backup")
    assert result["result"]["archive_integrity_verified"] is True
    assert result["result"]["restore_tested"] is False
    assert s.setup.save_and_backup("test-backup", idempotency_key="backup")["replayed"] is True
    s.scene.manager.ExportProject.assert_called_once()


def test_backup_rejects_traversal_and_wrong_scope_before_save(setup_scene):
    with pytest.raises(InvalidOperationError):
        setup_scene.setup.save_and_backup("../unsafe", idempotency_key="backup")


def test_backup_false_success_is_detected(setup_scene):
    s = setup_scene
    s.scene.manager.SaveProject = Mock(return_value=True)
    s.scene.manager.ExportProject = Mock(return_value=True)
    with pytest.raises(VerificationError, match="absent or empty"):
        s.setup.save_and_backup("missing-export", idempotency_key="backup")
