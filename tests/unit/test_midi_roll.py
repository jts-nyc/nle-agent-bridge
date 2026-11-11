"""MIDI piano-roll recipe: pure layout (times, chunks, caps) and the Fusion build
against fakes (plan has no writes, graph shape, keyframes, verification, replay)."""

from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resolve_bridge import midi
from resolve_bridge.errors import InvalidOperationError, ProjectScopeError, StaleSnapshotError, VerificationError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.midi_roll import (FIXED_NODES, MAX_NODES, NODES_PER_CHUNK, MidiRollService, RollTooLargeError,
                                      build_layout, expected_graph, _linear_keys)
from smf_writer import end, off, on, smf, tempo

DEMO = Path(__file__).resolve().parents[1] / "fixtures" / "demo-two-hands.mid"
PPQ = 480
LAYOUT = dict(fps=Fraction(24), render_start=0, render_end=239, frame_width=1920, frame_height=1080, clip_start_frame=86400)


def demo():
    return midi.parse(DEMO.read_bytes())


def scale(n_notes, spacing_ticks=PPQ // 4, pitch=lambda i: 60 + i % 12):
    events = []
    for i in range(n_notes):
        events += [on(i * spacing_ticks, pitch(i)), off(i * spacing_ticks + spacing_ticks // 2, pitch(i))]
    return smf([[tempo(0, 120), *events, end(n_notes * spacing_ticks)]])


def scroll_x(layout, k, frame):
    return layout["playhead_x"] + 0.5 + k - (frame - layout["render_start"]) / (24 * layout["seconds_visible"])


# ------------------------------------------------------------------ layout

def test_every_note_left_edge_hits_the_playhead_at_its_tempo_mapped_onset():
    song = demo()
    layout = build_layout(song, **LAYOUT)
    onsets = 0
    for chunk in layout["chunks"]:
        for note in chunk["notes"]:
            if note["onset"] is None:
                continue
            frame = (note["onset"] - layout["start_seconds"]) * 24
            assert abs(scroll_x(layout, chunk["index"], frame) - 0.5 + note["x"] - note["width"] / 2 - 0.25) < 1e-9
            onsets += 1
    assert onsets == layout["note_count"] == 22  # skip_drums defaults on
    # 100 BPM for 8 beats (4.8 s), then 80 BPM (0.75 s per beat) for the right hand's last three notes.
    rh = build_layout(song, track=2, **LAYOUT)
    starts = sorted(n["onset"] for c in rh["chunks"] for n in c["notes"] if n["onset"] is not None)
    assert len(starts) == 11
    assert starts[8] == pytest.approx(4.8) and starts[9] == pytest.approx(4.8 + 0.75) and starts[10] == pytest.approx(4.8 + 1.5)


def test_scroll_keys_are_two_linear_keys_at_the_render_ends():
    layout = build_layout(demo(), **LAYOUT)
    for chunk in layout["chunks"]:
        assert chunk["keys"] == {0: pytest.approx(scroll_x(layout, chunk["index"], 0)),
                                 239: pytest.approx(scroll_x(layout, chunk["index"], 239))}
    keys = _linear_keys({0: 1.0, 240: -1.0})
    assert keys[0] == {1: 1.0, "RH": {1: 80, 2: 1.0 - 2 / 3}, "Flags": {"Linear": True}}
    assert keys[240]["LH"] == {1: 160, 2: -1.0 + 2 / 3}


def test_note_crossing_a_chunk_boundary_is_split_and_only_the_first_piece_is_an_onset():
    # One 2-second note starting at 5 s with 6 s per chunk: pieces [5/6, 1) and [0, 1/6 - gap).
    song = midi.parse(smf([[tempo(0, 120), on(10 * PPQ, 60), off(14 * PPQ, 60), end(14 * PPQ)]]))
    layout = build_layout(song, **LAYOUT)
    first, second = layout["chunks"][0]["notes"][0], layout["chunks"][1]["notes"][0]
    assert first["onset"] == 5.0 and second["onset"] is None
    assert first["x"] - first["width"] / 2 == pytest.approx(5 / 6) and first["x"] + first["width"] / 2 == pytest.approx(1)
    assert second["x"] - second["width"] / 2 == pytest.approx(0)
    assert first["width"] + second["width"] == pytest.approx(2 / 6 - 0.002)
    assert layout["rect_count"] == 2 and layout["node_count"] == FIXED_NODES + 2 * NODES_PER_CHUNK + 2


def test_held_note_is_cut_where_it_leaves_the_visible_window():
    # A 60-second note on a 10-second clip: only 10 + 0.75 * 6 = 14.5 s can reach the screen.
    song = midi.parse(smf([[tempo(0, 120), on(0, 60), off(120 * PPQ, 60), end(120 * PPQ)]]))
    layout = build_layout(song, **LAYOUT)
    assert layout["chunk_count"] == 3 and layout["rect_count"] == 3
    last = layout["chunks"][-1]["notes"][0]
    assert last["x"] + last["width"] / 2 == pytest.approx(14.5 / 6 - 2)


def test_rows_pitch_padding_explicit_range_and_aspect():
    layout = build_layout(demo(), track=2, **LAYOUT)  # C4..G4 padded to an octave
    assert layout["rows"] == 12 and layout["min_pitch"] <= 60 and layout["max_pitch"] >= 67
    note = layout["chunks"][0]["notes"][0]
    row_h = (0.88 - 0.12) / 12
    assert note["height"] == pytest.approx(row_h * 0.8 * 1080 / 1920)
    narrow = build_layout(demo(), min_pitch=60, max_pitch=64, **LAYOUT)
    assert narrow["rows"] == 5
    assert {n["pitch"] for c in narrow["chunks"] for n in c["notes"]} <= {60, 61, 62, 63, 64}
    with pytest.raises(RollTooLargeError, match="rows"):
        build_layout(demo(), min_pitch=0, max_pitch=127, **LAYOUT)


def test_track_channel_drum_and_time_filters():
    song = demo()
    assert build_layout(song, skip_drums=False, **LAYOUT)["note_count"] == 40
    assert build_layout(song, channel=10, skip_drums=False, **LAYOUT)["note_count"] == 18
    late = build_layout(song, track=2, start_seconds=4.8, **LAYOUT)
    assert late["note_count"] == 3
    assert late["first_onsets"][0]["comp_frame"] == pytest.approx(0)
    with pytest.raises(InvalidOperationError, match="No notes"):
        build_layout(song, start_seconds=60.0, **LAYOUT)
    with pytest.raises(InvalidOperationError, match="track"):
        build_layout(song, track=9, **LAYOUT)


def test_bar_markers_follow_the_tempo_and_meter_changes():
    layout = build_layout(demo(), bar_markers=True, **LAYOUT)
    # Bars at 0, 2.4, 4.8 and 4.8 + 3 * 0.75 s; 24 fps; half frames round up.
    assert [m["timeline_frame"] - 86400 for m in layout["bar_markers"]] == [0, 58, 115, 169]
    assert [m["name"] for m in layout["bar_markers"]] == ["Bar 1", "Bar 2", "Bar 3", "Bar 4"]
    assert layout["bar_markers"][2]["note"].startswith("3/4")
    assert build_layout(demo(), **LAYOUT)["bar_markers"] == []


def test_caps_fail_with_counts_and_advice():
    song = midi.parse(scale(400, spacing_ticks=PPQ // 8))  # 400 notes in 25 s
    with pytest.raises(RollTooLargeError) as caught:
        build_layout(song, **{**LAYOUT, "render_end": 24 * 30 - 1})
    message = str(caught.value)
    assert "Fusion nodes" in message and f"caps are {MAX_NODES} nodes" in message and "Narrow" in message
    narrowed = build_layout(song, **{**LAYOUT, "render_end": 24 * 30 - 1}, end_seconds=8.0)
    assert narrowed["node_count"] <= MAX_NODES


@pytest.mark.parametrize("change", [{"seconds_visible": 1}, {"playhead_x": 0.95}, {"start_seconds": float("nan")},
                                    {"skip_drums": 1}, {"min_pitch": 70, "max_pitch": 60}, {"channel": 0},
                                    {"end_seconds": 0.0}, {"render_end": 10}])
def test_invalid_layout_arguments(change):
    with pytest.raises(InvalidOperationError):
        build_layout(demo(), **{**LAYOUT, **change})


# ------------------------------------------------------------------ Fusion fakes

OUTPUT_IDS = {"BezierSpline": "Value", "XYPath": "Value", "RectangleMask": "Mask", "AudioDisplay": "Data"}


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
        return SimpleNamespace(GetTool=lambda: self.source, GetAttrs=lambda: {"OUTS_ID": OUTPUT_IDS.get(self.source.kind, "Output")})


class Tool:
    def __init__(self, comp, kind, name):
        self.comp, self.kind, self.name = comp, kind, name
        self.inputs, self.values, self.keys, self.bypass = {}, {}, {}, False
        self.SetInput = Mock(side_effect=lambda key, value: self.values.update({key: deepcopy(value)}))
        self.SetKeyFrames = Mock(side_effect=lambda keys, replace: setattr(self, "keys", deepcopy(keys)))

    def GetAttrs(self):
        return {"TOOLS_Name": self.name, "TOOLS_RegID": self.kind, "TOOLB_PassThrough": self.bypass}

    def SetAttrs(self, attrs):
        self.name = attrs["TOOLS_Name"]

    def GetInputList(self):
        return dict(enumerate(self.inputs.values(), 1))

    def ConnectInput(self, key, source):
        self.inputs[key] = Input(key, source)
        return True

    def AddModifier(self, key, kind):
        self.inputs[key] = Input(key, self.comp.AddTool(kind, False, 0, 0))
        return True

    def value_at(self, time):
        """A spline's value: piecewise linear, like the handles the recipe writes."""
        frames = sorted(self.keys)
        if time <= frames[0]:
            return self.keys[frames[0]][1]
        for left, right in zip(frames, frames[1:]):
            if left <= time <= right:
                a, b = self.keys[left][1], self.keys[right][1]
                return a + (b - a) * (time - left) / (right - left)
        return self.keys[frames[-1]][1]

    def GetInput(self, key, time):
        source = self.inputs[key].source if key in self.inputs else None
        if source is not None and source.kind == "BezierSpline":
            return source.value_at(time)
        if source is not None and source.kind == "XYPath":
            return {1: source.GetInput("X", time), 2: source.values.get("Y")}
        return deepcopy(self.values.get(key))

    def GetKeyFrames(self):
        return deepcopy(self.keys)


class Comp:
    def __init__(self):
        self.tools, self.locked = [], False
        self.attrs = {"COMPN_RenderStart": 0, "COMPN_RenderEnd": 239, "COMPN_GlobalStart": -90, "COMPN_GlobalEnd": 819,
                      "COMPS_Name": "TestComp"}
        self.AddTool = Mock(side_effect=self.add)
        self.StartUndo, self.EndUndo = Mock(), Mock()
        self.GetPrefs = Mock(side_effect=lambda key: {"Comp.FrameFormat.Width": 1920, "Comp.FrameFormat.Height": 1080}[key])
        source, out = self.add("MediaIn", False, 0, 0), self.add("MediaOut", False, 0, 0)
        out.ConnectInput("Input", source)

    def add(self, kind, defaults, x, y):
        tool = Tool(self, kind, kind + str(len(self.tools) + 1))
        self.tools.append(tool)
        return tool

    def CopySettings(self, tools):
        result = {}
        for tool in tools.values():
            inputs = {key: {"__ctor": "Input", "Value": value} for key, value in tool.values.items()}
            for key, inp in tool.inputs.items():
                if inp.source:
                    inputs[key] = {"__ctor": "Input", "SourceOp": inp.source.name, "Source": OUTPUT_IDS.get(inp.source.kind, "Output")}
            result[tool.name] = {"__ctor": tool.kind, "Inputs": inputs, "KeyFrames": tool.keys}
        return deepcopy({"Tools": result})

    def GetAttrs(self):
        return self.attrs.copy()

    def GetToolList(self, selected):
        assert selected is False
        return dict(enumerate(self.tools, 1))

    def IsLocked(self):
        return self.locked


@pytest.fixture
def roll(scene, tmp_path):
    scene.timeline.GetName.return_value = "AI_BRIDGE_TEST_MIDI"
    comp = Comp()
    scene.item.GetFusionCompCount = Mock(return_value=1)
    scene.item.GetFusionCompNameList = Mock(return_value=["Composition 1"])
    scene.item.GetFusionCompByName = Mock(return_value=comp)
    scene.resolve.GetCurrentPage = Mock(return_value="fusion")
    scene.resolve.Fusion = Mock(return_value=SimpleNamespace(GetCurrentComp=Mock(return_value=comp)))
    scene.timeline.GetCurrentVideoItem = Mock(return_value=scene.item)
    scene.timeline.GetMarkers.return_value = {}

    def marker(frame, color, name, note, duration, custom):
        scene.timeline.GetMarkers.return_value[frame] = {"color": color, "name": name, "note": note,
                                                         "duration": duration, "customData": custom}
        return True
    scene.timeline.AddMarker = Mock(side_effect=marker)
    path = tmp_path / "song.mid"
    path.write_bytes(DEMO.read_bytes())
    service = MidiRollService(scene.adapter, "project-1", "timeline-1", OperationJournal(tmp_path / "roll.db"))
    return SimpleNamespace(scene=scene, comp=comp, service=service, path=path)


def plan(roll, **changes):
    graph = roll.service.read_graph("clip-1", "Composition 1")
    args = {"clip_id": "clip-1", "comp_name": "Composition 1", "midi_path": str(roll.path),
            "expected_snapshot_id": roll.scene.adapter.read_timeline().snapshot_id, "expected_graph_id": graph["graph_id"]}
    return roll.service.plan_midi_roll(**(args | changes))


def test_plan_reads_only_and_summarizes(roll):
    proposed, layout = plan(roll)
    roll.comp.AddTool.assert_not_called()
    roll.scene.timeline.AddMarker.assert_not_called()
    assert proposed.midi_sha256 and proposed.layout_sha256
    assert layout["note_count"] == 22 and layout["node_count"] == FIXED_NODES + NODES_PER_CHUNK * layout["chunk_count"] + layout["rect_count"]


def test_execute_builds_the_planned_graph_and_replays(roll):
    proposed, layout = plan(roll)
    before = {t.name for t in roll.comp.tools}
    result = roll.service.execute_midi_roll(proposed, idempotency_key="roll")
    assert result["status"] == "verified"
    body = result["result"]
    assert body["added_nodes"] == layout["node_count"] == len(roll.comp.tools) - len(before)
    assert body["visual_verification"] == "not_performed"
    assert body["onset_checks"] and all(abs(c["left_edge_x"] - 0.25) < 1e-9 for c in body["onset_checks"])
    graph = roll.service.read_graph("clip-1", "Composition 1")
    types, edges = expected_graph(layout, {"MediaIn": "MediaIn1", "MediaOut": "MediaOut2"})
    assert {n["name"]: n["type"] for n in graph["nodes"] if n["name"] not in before} == types
    assert {(e["source"], e["output"], e["target"], e["input"]) for e in graph["connections"]} == edges
    scroll = next(n for n in graph["nodes"] if n["name"] == "AI_Roll_Scroll_00")
    assert set(scroll["keyframes"]) == {"0", "239"} and scroll["keyframes"]["0"]["Flags"] == {"Linear": True}
    roll.comp.EndUndo.assert_called_once_with(True)
    calls = roll.comp.AddTool.call_count
    assert roll.service.execute_midi_roll(proposed, idempotency_key="roll")["replayed"]
    assert roll.comp.AddTool.call_count == calls


def test_bar_markers_are_added_and_verified(roll):
    proposed, layout = plan(roll, bar_markers=True)
    result = roll.service.execute_midi_roll(proposed, idempotency_key="bars")["result"]
    assert [m["offset_frame"] for m in result["bar_markers"]] == [0, 58, 115, 169]
    assert sorted(roll.scene.timeline.GetMarkers.return_value) == [0, 58, 115, 169]
    assert all(m["custom_data"].endswith(f":bar:{i}") for i, m in enumerate(result["bar_markers"], 1))


def test_existing_marker_on_a_bar_frame_refuses_the_plan(roll):
    roll.scene.timeline.GetMarkers.return_value = {58: {"color": "Blue", "name": "client note", "note": "", "duration": 1, "customData": ""}}
    with pytest.raises(InvalidOperationError, match="already has a marker"):
        plan(roll, bar_markers=True)


def test_midi_file_changed_after_planning_is_stale(roll):
    proposed, _ = plan(roll)
    roll.path.write_bytes(smf([[on(0, 60), off(PPQ, 60), end(PPQ)]]))
    with pytest.raises(StaleSnapshotError, match="MIDI file changed"):
        roll.service.execute_midi_roll(proposed, idempotency_key="stale")
    roll.comp.AddTool.assert_not_called()


@pytest.mark.parametrize("change", [{"layout_sha256": "0" * 64}, {"seconds_visible": 50.0}, {"midi_path": "/etc/passwd"}])
def test_forged_plan_is_revalidated(roll, change):
    proposed = replace(plan(roll)[0], **change)
    with pytest.raises((InvalidOperationError, StaleSnapshotError)):
        roll.service.execute_midi_roll(proposed, idempotency_key="forged")
    roll.comp.AddTool.assert_not_called()


def test_over_cap_fails_at_plan_before_any_write(roll):
    roll.path.write_bytes(scale(400, spacing_ticks=PPQ // 8))
    with pytest.raises(RollTooLargeError, match="Fusion nodes"):
        plan(roll)
    roll.comp.AddTool.assert_not_called()


def test_scope_rules_match_the_lower_third(roll):
    proposed, _ = plan(roll)
    with pytest.raises(ProjectScopeError):
        roll.service.execute_midi_roll(replace(proposed, project_id="other-project"), idempotency_key="scope")
    roll.scene.timeline.GetName.return_value = "Timeline 1"
    with pytest.raises(InvalidOperationError, match="AI_BRIDGE_TEST_"):
        plan(roll)
    roll.scene.timeline.GetName.return_value = "AI_BRIDGE_TEST_MIDI"
    roll.comp.add("Blur", False, 0, 0)
    with pytest.raises(InvalidOperationError, match="MediaIn"):
        plan(roll)
    roll.comp.AddTool.assert_not_called()


def test_eased_scroll_is_caught_by_onset_evaluation(roll):
    proposed, _ = plan(roll)
    original = roll.comp.AddTool.side_effect

    def eased(kind, *args):
        tool = original(kind, *args)
        if kind == "BezierSpline":
            # An eased curve: right values at the keys, wrong in between.
            tool.value_at = lambda time, tool=tool: Tool.value_at(tool, time) + 0.01 * (0 < time < 239)
        return tool
    roll.comp.AddTool.side_effect = eased
    with pytest.raises(VerificationError, match="scroll is off the plan"):
        roll.service.execute_midi_roll(proposed, idempotency_key="eased")
    roll.comp.EndUndo.assert_called_once_with(True)


def test_mask_value_mismatch_is_detected(roll):
    proposed, _ = plan(roll)
    original = roll.comp.AddTool.side_effect

    def bad(kind, *args):
        tool = original(kind, *args)
        if kind == "RectangleMask" and len(roll.comp.tools) == 4:
            setter = tool.SetInput.side_effect
            tool.SetInput.side_effect = lambda k, v: setter(k, v * 2 if k == "Width" else v)
        return tool
    roll.comp.AddTool.side_effect = bad
    with pytest.raises(VerificationError, match="position or size"):
        roll.service.execute_midi_roll(proposed, idempotency_key="bad-width")


def test_wiring_mismatch_is_reported_with_the_edges(roll):
    proposed, _ = plan(roll)
    original = roll.comp.AddTool.side_effect

    def miswire(kind, *args):
        tool = original(kind, *args)
        if kind == "Background":
            connect = tool.ConnectInput
            tool.ConnectInput = lambda key, source: connect("Mask" if key == "EffectMask" else key, source)
        return tool
    roll.comp.AddTool.side_effect = miswire
    with pytest.raises(VerificationError, match="wiring differs.*missing"):
        roll.service.execute_midi_roll(proposed, idempotency_key="miswired")


def test_partial_failure_keeps_undo_and_blocks_retry(roll):
    from resolve_bridge.errors import OperationUncertainError, ResolveWriteError
    proposed, _ = plan(roll)
    original = roll.comp.AddTool.side_effect

    def fail_on_merge(kind, *args):
        if kind == "Merge":
            raise RuntimeError("connection lost")
        return original(kind, *args)
    roll.comp.AddTool.side_effect = fail_on_merge
    with pytest.raises(ResolveWriteError):
        roll.service.execute_midi_roll(proposed, idempotency_key="partial")
    roll.comp.EndUndo.assert_called_once_with(True)
    with pytest.raises(OperationUncertainError):
        roll.service.execute_midi_roll(proposed, idempotency_key="partial")


def test_locked_composition_is_refused(roll):
    proposed, _ = plan(roll)
    roll.comp.locked = True
    with pytest.raises(InvalidOperationError, match="already locked"):
        roll.service.execute_midi_roll(proposed, idempotency_key="locked")
    roll.comp.AddTool.assert_not_called()
