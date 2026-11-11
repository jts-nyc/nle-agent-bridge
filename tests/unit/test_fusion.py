from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resolve_bridge.errors import (InvalidOperationError, OperationUncertainError, ProjectScopeError,
    ResolveWriteError, StaleSnapshotError, UnexpectedAPIResponseError, VerificationError)
from resolve_bridge.fusion import FusionService, _await_value, _primitive
from resolve_bridge.journal import OperationJournal


class Input:
    def __init__(self, name, source=None):
        self.name, self.source, self.expression = name, source, None

    def GetAttrs(self):
        return {"INPS_ID": self.name}

    def GetExpression(self):
        return self.expression

    def GetConnectedOutput(self):
        if self.source is None:
            return None
        return SimpleNamespace(GetTool=lambda: self.source, GetAttrs=lambda: {
            "OUTS_ID": {"BezierSpline": "Value", "AudioDisplay": "Data"}.get(self.source.kind, "Output")})


class Tool:
    def __init__(self, comp, kind, name):
        self.comp, self.kind, self.name = comp, kind, name
        self.inputs, self.values, self.keys = {}, {}, {}
        self.bypass = False
        self.GetAttrs = Mock(side_effect=lambda: {"TOOLS_Name": self.name, "TOOLS_RegID": self.kind, "TOOLB_PassThrough": self.bypass})
        self.SetInput = Mock(side_effect=lambda key, value: self.values.update({key: deepcopy(value)}))

    def GetInputList(self):
        return dict(enumerate(self.inputs.values(), 1))

    def GetInput(self, key, time):
        if key == "Blend" and "Blend" in self.inputs:
            keys = self.inputs["Blend"].source.keys
            ordered = sorted(keys)
            for left, right in zip(ordered, ordered[1:]):
                if left <= time <= right:
                    return keys[left][1] + (keys[right][1] - keys[left][1]) * (time - left) / (right - left)
        return self.values.get(key)

    def SetAttrs(self, attrs):
        self.name = attrs["TOOLS_Name"]

    def ConnectInput(self, key, source):
        self.inputs[key] = Input(key, source)
        return True

    def AddModifier(self, key, kind):
        source = self.comp.AddTool(kind, False, 0, 0)
        self.inputs[key] = Input(key, source)
        if self.kind == "Merge" and key == "Blend":
            self.inputs["BlendClone"] = Input("BlendClone", source)
        return True

    def SetKeyFrames(self, keys, replace):
        self.keys = deepcopy(keys)

    def GetKeyFrames(self):
        return deepcopy(self.keys)


class Comp:
    def __init__(self):
        self.tools, self.locked = [], False
        self.attrs = {"COMPN_RenderStart": 0, "COMPN_RenderEnd": 239,
                      "COMPN_GlobalStart": -90, "COMPN_GlobalEnd": 819, "COMPS_Name": "TestComp"}
        self.AddTool = Mock(side_effect=self.add)
        self.StartUndo, self.EndUndo = Mock(), Mock()
        self.Unlock = Mock(side_effect=lambda: setattr(self, "locked", False))
        source = self.add("MediaIn", False, 0, 0)
        out = self.add("MediaOut", False, 0, 0)
        out.ConnectInput("Input", source)
        self.CopySettings = Mock(side_effect=self.copy)

    def add(self, kind, defaults, x, y):
        tool = Tool(self, kind, kind + "1")
        self.tools.append(tool)
        return tool

    def copy(self, tools):
        result = {}
        for tool in tools.values():
            inputs = {key: {"__ctor": "Input", "Value": value} for key, value in tool.values.items()}
            for key, inp in tool.inputs.items():
                if inp.source:
                    inputs[key] = {"__ctor": "Input", "SourceOp": inp.source.name,
                                   "Source": "Value" if inp.source.kind == "BezierSpline" else "Output"}
                if inp.expression:
                    inputs[key] = {"Expression": inp.expression}
            result[tool.name] = {"__ctor": tool.kind, "Inputs": inputs}
            if tool.kind == "BezierSpline":
                result[tool.name]["KeyFrames"] = tool.keys
        return deepcopy({"Tools": result})

    def GetAttrs(self):
        return self.attrs.copy()

    def GetToolList(self, selected):
        assert selected is False
        return dict(enumerate(self.tools, 1))

    def IsLocked(self):
        return self.locked

    def Lock(self):
        self.locked = True


@pytest.fixture
def fusion(scene, tmp_path):
    scene.timeline.GetName.return_value = "AI_BRIDGE_TEST_FUSION"
    comp = Comp()
    scene.item.GetFusionCompCount = Mock(return_value=1)
    scene.item.GetFusionCompNameList = Mock(return_value=["Composition 1"])
    scene.item.GetFusionCompByName = Mock(return_value=comp)
    scene.item.AddFusionComp = Mock(return_value=comp)
    scene.item.LoadFusionCompByName = Mock(return_value=comp)
    scene.resolve.GetCurrentPage = Mock(return_value="fusion")
    def open_page(page):
        scene.resolve.GetCurrentPage.return_value = page
        return True
    scene.resolve.OpenPage = Mock(side_effect=open_page)
    scene.resolve.Fusion = Mock(return_value=SimpleNamespace(GetCurrentComp=Mock(return_value=comp)))
    scene.timeline.GetCurrentVideoItem = Mock(return_value=scene.item)
    scene.timeline.GetCurrentTimecode = Mock(return_value="01:00:02:00")
    def position(tc):
        scene.timeline.GetCurrentTimecode.return_value = tc
        return True
    scene.timeline.SetCurrentTimecode = Mock(side_effect=position)
    service = FusionService(scene.adapter, "project-1", "timeline-1", OperationJournal(tmp_path / "fusion.db"))
    return SimpleNamespace(scene=scene, comp=comp, service=service)


def plan(fusion, **changes):
    graph = fusion.service.read_graph("clip-1", "Composition 1")
    args = {"clip_id": "clip-1", "comp_name": "Composition 1", "text": "Hello Fusion",
            "expected_snapshot_id": fusion.scene.adapter.read_timeline().snapshot_id,
            "expected_graph_id": graph["graph_id"]}
    return fusion.service.plan_lower_third(**(args | changes))


def test_normalized_graph_and_plan_have_no_writes(fusion):
    graph = fusion.service.read_graph("clip-1", "Composition 1")
    assert graph["render_start"] == 0 and graph["global_start"] == -90
    assert graph["connections"] == [{"source": "MediaIn1", "output": "Output", "target": "MediaOut1", "input": "Input"}]
    assert graph == fusion.service.read_graph("clip-1", "Composition 1")
    plan(fusion)
    fusion.comp.AddTool.assert_not_called()
    fusion.scene.item.AddFusionComp.assert_not_called()


def test_verified_graph_keyframes_and_idempotency(fusion):
    proposed = plan(fusion)
    result = fusion.service.execute_lower_third(proposed, idempotency_key="effect")
    assert result["status"] == "verified"
    assert result["result"]["keyframes"] == [{"frame": 0, "value": 0}, {"frame": 12, "value": 1}, {"frame": 227, "value": 1}, {"frame": 239, "value": 0}]
    assert fusion.comp.AddTool.call_count == 3
    assert fusion.service.execute_lower_third(proposed, idempotency_key="effect")["replayed"]
    assert fusion.comp.AddTool.call_count == 3
    fusion.comp.EndUndo.assert_called_once_with(True)
    assert not fusion.comp.locked


@pytest.mark.parametrize("change", [{"text": ""}, {"text": "a" * 161}, {"text": "a\nb\nc"},
    {"text": "a\x00b"}, {"x": float("nan")}, {"x": True}, {"y": 0.8}, {"size": 0},
    {"fade_frames": True}, {"fade_frames": 0}, {"fade_frames": 120}])
def test_invalid_recipe_never_mutates(fusion, change):
    with pytest.raises(InvalidOperationError):
        plan(fusion, **change)
    fusion.comp.AddTool.assert_not_called()


def test_stale_graph_including_unlisted_parameters_rejected(fusion):
    proposed = plan(fusion)
    fusion.comp.tools[0].values["UnlistedParameter"] = 7
    with pytest.raises(StaleSnapshotError):
        fusion.service.execute_lower_third(proposed, idempotency_key="effect")
    fusion.comp.AddTool.assert_not_called()


def test_timeline_edit_invalidates_plan(fusion):
    proposed = plan(fusion)
    fusion.scene.item.GetStart.return_value += 1
    with pytest.raises(StaleSnapshotError):
        fusion.service.execute_lower_third(proposed, idempotency_key="effect")
    fusion.comp.AddTool.assert_not_called()


@pytest.mark.parametrize("change", ["name", "locked", "disabled", "version", "project"])
def test_write_scope_rejected(fusion, change):
    if change == "name":
        fusion.scene.timeline.GetName.return_value = "Original timeline"
    elif change == "locked":
        fusion.scene.timeline.GetIsTrackLocked.return_value = True
    elif change == "disabled":
        fusion.scene.timeline.GetIsTrackEnabled.return_value = False
    elif change == "version":
        fusion.scene.resolve.GetVersionString.return_value = "future-version"
    else:
        fusion.scene.project.GetUniqueId.return_value = "other-project"
    with pytest.raises((InvalidOperationError, ProjectScopeError)):
        plan(fusion)
    fusion.comp.AddTool.assert_not_called()


@pytest.mark.parametrize("change", ["extra_node", "disconnect", "expression", "bypass"])
def test_existing_custom_graph_is_preserved(fusion, change):
    if change == "extra_node":
        fusion.comp.add("Blur", False, 0, 0)
    elif change == "disconnect":
        fusion.comp.tools[1].inputs["Input"].source = None
    elif change == "expression":
        fusion.comp.tools[1].inputs["Input"].expression = "arbitrary()"
    else:
        fusion.comp.tools[0].bypass = True
    with pytest.raises(InvalidOperationError):
        plan(fusion)
    fusion.comp.AddTool.assert_not_called()


def test_partial_failure_keeps_undo_and_blocks_retry(fusion):
    proposed = plan(fusion)
    original = fusion.comp.AddTool.side_effect
    def fail_after_first(kind, *args):
        if kind == "Merge":
            raise RuntimeError("connection lost")
        return original(kind, *args)
    fusion.comp.AddTool.side_effect = fail_after_first
    with pytest.raises(ResolveWriteError):
        fusion.service.execute_lower_third(proposed, idempotency_key="partial")
    assert len(fusion.comp.tools) == 3
    fusion.comp.EndUndo.assert_called_once_with(True)
    assert not fusion.comp.locked
    with pytest.raises(OperationUncertainError):
        fusion.service.execute_lower_third(proposed, idempotency_key="partial")
    assert fusion.comp.AddTool.call_count == 2


def test_existing_composition_lock_is_preserved(fusion):
    proposed = plan(fusion)
    fusion.comp.locked = True
    with pytest.raises(InvalidOperationError, match="already locked"):
        fusion.service.execute_lower_third(proposed, idempotency_key="locked-comp")
    assert fusion.comp.locked
    fusion.comp.Unlock.assert_not_called()
    fusion.comp.AddTool.assert_not_called()


def test_false_parameter_success_is_detected(fusion):
    proposed = plan(fusion)
    original = fusion.comp.AddTool.side_effect
    def create(kind, *args):
        tool = original(kind, *args)
        if kind == "TextPlus":
            setter = tool.SetInput.side_effect
            tool.SetInput.side_effect = lambda k, v: setter(k, "wrong" if k == "StyledText" else v)
        return tool
    fusion.comp.AddTool.side_effect = create
    with pytest.raises(VerificationError, match="Text"):
        fusion.service.execute_lower_third(proposed, idempotency_key="bad-text")
    assert not fusion.comp.locked


def test_invalid_forged_plan_revalidated(fusion):
    proposed = replace(plan(fusion), size=500)
    with pytest.raises(InvalidOperationError):
        fusion.service.execute_lower_third(proposed, idempotency_key="forged")
    fusion.comp.AddTool.assert_not_called()


def test_native_serialization_failure_is_not_empty_graph(fusion):
    fusion.comp.CopySettings.side_effect = lambda tools: {"Tools": None}
    with pytest.raises(UnexpectedAPIResponseError):
        fusion.service.read_graph("clip-1", "Composition 1")


def test_graph_change_during_read_fails(fusion):
    calls = 0
    def changing(tools):
        nonlocal calls
        calls += 1
        result = fusion.comp.copy(tools)
        result["revision"] = calls
        return result
    fusion.comp.CopySettings.side_effect = changing
    with pytest.raises(StaleSnapshotError):
        fusion.service.read_graph("clip-1", "Composition 1")


@pytest.mark.parametrize("value", [float("nan"), object(), {1: "a", "1": "b"}])
def test_normalizer_rejects_lossy_or_opaque_values(value):
    with pytest.raises(UnexpectedAPIResponseError):
        _primitive(value)


def test_composition_creation_refuses_existing_comps(fusion):
    with pytest.raises(InvalidOperationError):
        fusion.service.create_composition("clip-1", expected_snapshot_id=fusion.scene.adapter.read_timeline().snapshot_id, idempotency_key="create")
    fusion.scene.item.AddFusionComp.assert_not_called()


@pytest.mark.parametrize("empty", [{}, None, []])
def test_create_composition_verifies_count_and_replays(fusion, empty):
    fusion.scene.item.GetFusionCompCount.return_value = 0
    fusion.scene.item.GetFusionCompNameList.return_value = empty
    def create():
        fusion.scene.item.GetFusionCompCount.return_value = 1
        fusion.scene.item.GetFusionCompNameList.return_value = ["Composition 1"]
        return fusion.comp
    fusion.scene.item.AddFusionComp.side_effect = create
    kwargs = dict(expected_snapshot_id=fusion.scene.adapter.read_timeline().snapshot_id, idempotency_key="create")
    assert fusion.service.create_composition("clip-1", **kwargs)["status"] == "verified"
    assert fusion.service.create_composition("clip-1", **kwargs)["replayed"]
    fusion.scene.item.AddFusionComp.assert_called_once()


def test_none_names_with_nonzero_count_is_an_error(fusion):
    fusion.scene.item.GetFusionCompNameList.return_value = None
    with pytest.raises(UnexpectedAPIResponseError):
        fusion.service.list_compositions("clip-1")


def test_unloaded_graph_cannot_report_success(fusion):
    proposed = plan(fusion)
    fusion.scene.resolve.GetCurrentPage.return_value = "edit"
    with pytest.raises(InvalidOperationError, match="Prepare"):
        fusion.service.execute_lower_third(proposed, idempotency_key="unloaded")
    fusion.comp.AddTool.assert_not_called()


def test_prepare_and_restore_explicit_view(fusion):
    fusion.scene.resolve.GetCurrentPage.return_value = "edit"
    saved = fusion.service.prepare_composition("clip-1", "Composition 1", idempotency_key="prepare")["result"]
    assert saved["page"] == "edit" and saved["timecode"] == "01:00:02:00"
    assert fusion.scene.resolve.GetCurrentPage() == "fusion"
    fusion.service.restore_view(saved, idempotency_key="restore")
    assert fusion.scene.resolve.GetCurrentPage() == "edit"
    assert fusion.scene.timeline.GetCurrentTimecode() == "01:00:02:00"


def add_audio_helpers(comp):
    source = comp.tools[0]
    for side in ("Left", "Right"):
        helper = comp.add("AudioDisplay", False, 0, 0)
        helper.name = side
        source.ConnectInput(side + "Audio", helper)


def test_host_audio_helpers_are_preserved(fusion):
    add_audio_helpers(fusion.comp)
    before = fusion.comp.copy(fusion.comp.GetToolList(False))
    result = fusion.service.execute_lower_third(plan(fusion), idempotency_key="with-audio")
    assert result["status"] == "verified"
    after = fusion.comp.copy(fusion.comp.GetToolList(False))
    for name in ("MediaIn1", "Left", "Right"):
        assert after["Tools"][name] == before["Tools"][name]


@pytest.mark.parametrize("change", ["extra_helper", "missing_connection", "wrong_connection"])
def test_custom_audio_graph_is_rejected(fusion, change):
    add_audio_helpers(fusion.comp)
    if change == "extra_helper":
        fusion.comp.add("AudioDisplay", False, 0, 0)
    elif change == "missing_connection":
        del fusion.comp.tools[0].inputs["LeftAudio"]
    else:
        fusion.comp.tools[0].inputs["LeftAudio"].source = fusion.comp.tools[3]
    with pytest.raises(InvalidOperationError):
        plan(fusion)
    fusion.comp.AddTool.assert_not_called()


def test_prepare_positions_from_fusion_through_edit(fusion):
    position = fusion.scene.timeline.SetCurrentTimecode.side_effect
    def edit_only(tc):
        assert fusion.scene.resolve.GetCurrentPage() == "edit"
        return position(tc)
    fusion.scene.timeline.SetCurrentTimecode.side_effect = edit_only
    saved = fusion.service.prepare_composition("clip-1", "Composition 1", idempotency_key="from-fusion")["result"]
    assert saved["page"] == "fusion"
    fusion.service.restore_view(saved, idempotency_key="to-fusion")
    assert fusion.scene.resolve.GetCurrentPage() == "fusion"
    assert fusion.scene.timeline.GetCurrentTimecode() == saved["timecode"]


def test_delayed_ui_readback_is_polled(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("resolve_bridge.fusion.time.sleep", sleep)
    read = Mock(side_effect=["fusion", "fusion", "edit"])
    _await_value(read, "edit", "page")
    assert sleep.call_count == 2


def test_unacknowledged_ui_readback_fails():
    with pytest.raises(VerificationError, match="expected 'edit', got 'fusion'"):
        _await_value(lambda: "fusion", "edit", "page", timeout=0)
