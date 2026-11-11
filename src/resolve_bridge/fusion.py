"""Scoped Fusion reads and one bounded recipe: an animated Text+ lower third.

No evaluator, arbitrary node type, expression, or general parameter setter is
exposed. Native objects stay here. Mutations currently require a test timeline.
"""

from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import sys
import time

from . import calibration
from ._api import _call, _id, _integer, _list, _text, _write
from .errors import (InvalidOperationError, ProjectScopeError, StaleSnapshotError, UncalibratedBuildError,
                     UnexpectedAPIResponseError, VerificationError)
from .snapshot import require_snapshot
from .timecode import frames_to_timecode


PARAMETERS = {
    "TextPlus": ("StyledText", "Font", "Style", "Size", "Center"),
    "Merge": ("Blend", "Center", "Size"),
    # Read back by the MIDI piano-roll recipe (midi_roll.py).
    "RectangleMask": ("Center", "Width", "Height"),
    "Background": ("TopLeftRed", "TopLeftGreen", "TopLeftBlue", "TopLeftAlpha"),
    "XYPath": ("Y",),
    "BrightnessContrast": ("Gain",),
}
TEXT_NAME = "AI_Bridge_Text"
MERGE_NAME = "AI_Bridge_Merge"
SPLINE_NAME = "AI_Bridge_Opacity"


def _table(value, field):
    if not isinstance(value, dict):
        raise UnexpectedAPIResponseError(f"{field} must return a table.")
    return value


def _primitive(value):
    """Normalize Lua tables without stringifying native objects or key collisions."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value) if value == int(value) else value
    if isinstance(value, (list, tuple)):
        return [_primitive(v) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str):
                normalized = key
            elif type(key) in (int, float) and math.isfinite(key):
                normalized = str(int(key)) if key == int(key) else str(key)
            else:
                raise UnexpectedAPIResponseError("Unsupported Fusion table key.")
            if normalized in result:
                raise UnexpectedAPIResponseError("Colliding Fusion table keys.")
            result[normalized] = _primitive(item)
        return result
    raise UnexpectedAPIResponseError("Fusion returned an unsupported or nonfinite value.")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _copy_settings(comp, tools):
    # Resolve 21's native wrapper evaluates serialized OrderedDict constructors
    # in __main__. Without this type it prints NameError and returns Tools=None.
    namespace = vars(sys.modules["__main__"])
    if namespace.setdefault("OrderedDict", OrderedDict) is not OrderedDict:
        raise UnexpectedAPIResponseError("Fusion requires collections.OrderedDict in __main__.")
    settings = _primitive(_table(_call(comp, "CopySettings", tools), "CopySettings"))
    if not isinstance(settings.get("Tools"), dict):
        raise UnexpectedAPIResponseError("CopySettings did not serialize the Fusion tools.")
    return settings


def _number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise InvalidOperationError(f"{name} must be a finite number between {low} and {high}.")


def _await_value(read, expected, field, timeout=3):
    """Wait for an acknowledged UI change without repeating the mutation."""
    deadline = time.monotonic() + timeout
    while True:
        actual = read()
        if actual == expected:
            return
        if time.monotonic() >= deadline:
            raise VerificationError(f"{field} did not settle: expected {expected!r}, got {actual!r}.")
        time.sleep(0.05)


@dataclass(frozen=True)
class LowerThirdPlan:
    project_id: str
    timeline_id: str
    clip_id: str
    comp_name: str
    expected_snapshot_id: str
    expected_graph_id: str
    text: str
    x: float
    y: float
    size: float
    fade_frames: int

    def to_dict(self):
        return asdict(self)


class FusionAdapter:
    def __init__(self, adapter, project_id: str, timeline_id: str):
        if not all(isinstance(v, str) and v for v in (project_id, timeline_id)):
            raise InvalidOperationError("Fusion requires explicit project and timeline IDs.")
        self._adapter = adapter
        self.project_id, self.timeline_id = project_id, timeline_id

    def _target(self, clip_id):
        project = self._adapter._project()
        if _id(project) != self.project_id:
            raise ProjectScopeError("The open project is outside the Fusion scope.")
        timeline = self._adapter._timeline_by_id(project, self.timeline_id)
        matches = []
        for index in range(1, _integer(_call(timeline, "GetTrackCount", "video"), "GetTrackCount") + 1):
            matches.extend((item, index) for item in _list(_call(timeline, "GetItemListInTrack", "video", index), "GetItemListInTrack") if _id(item) == clip_id)
        if len(matches) != 1:
            raise InvalidOperationError("Fusion requires one video clip with the requested stable ID.")
        return timeline, matches[0][0], matches[0][1]

    def list_compositions(self, clip_id: str) -> tuple[str, ...]:
        _, item, _ = self._target(clip_id)
        count = _integer(_call(item, "GetFusionCompCount"), "GetFusionCompCount")
        raw_names = _call(item, "GetFusionCompNameList")
        # Resolve 21 returns {} for a clip with zero compositions. Only treat
        # it as empty when the independent count confirms that case.
        if count == 0 and (raw_names is None or raw_names == {}):
            raw_names = []
        names = tuple(_text(n, "Fusion composition name") for n in _list(raw_names, "GetFusionCompNameList"))
        if count != len(names) or len(set(names)) != len(names):
            raise UnexpectedAPIResponseError("Fusion composition names/count disagree or are ambiguous.")
        self._target(clip_id)
        return names

    def _comp(self, clip_id, comp_name):
        if comp_name not in self.list_compositions(clip_id):
            raise InvalidOperationError("The named Fusion composition is missing.")
        _, item, _ = self._target(clip_id)
        comp = _call(item, "GetFusionCompByName", comp_name)
        if comp is None:
            raise UnexpectedAPIResponseError("GetFusionCompByName returned no composition.")
        return comp

    def read_graph(self, clip_id: str, comp_name: str) -> dict:
        """Read even an inactive timeline, without selecting it or moving the playhead.

        graph_id covers complete copied tool settings, topology, scope and frame
        ranges. This is optimistic concurrency detection, not a GUI edit lock.
        """
        comp = self._comp(clip_id, comp_name)
        attrs = _table(_call(comp, "GetAttrs"), "Composition.GetAttrs")
        ranges = {key: _integer(attrs.get(native), native) for key, native in (
            ("render_start", "COMPN_RenderStart"), ("render_end", "COMPN_RenderEnd"),
            ("global_start", "COMPN_GlobalStart"), ("global_end", "COMPN_GlobalEnd"))}
        tools = _table(_call(comp, "GetToolList", False), "GetToolList")
        settings = _copy_settings(comp, tools)
        nodes, edges = [], []
        for tool in tools.values():
            attr = _table(_call(tool, "GetAttrs"), "Tool.GetAttrs")
            name, kind = _text(attr.get("TOOLS_Name"), "TOOLS_Name"), _text(attr.get("TOOLS_RegID"), "TOOLS_RegID")
            inputs = _table(_call(tool, "GetInputList"), "GetInputList")
            expressions = {}
            for inp in inputs.values():
                ia = _table(_call(inp, "GetAttrs"), "Input.GetAttrs")
                input_id = _text(ia.get("INPS_ID"), "INPS_ID")
                expression = _call(inp, "GetExpression")
                if expression:
                    expressions[input_id] = _text(expression, "GetExpression")
                out = _call(inp, "GetConnectedOutput")
                if out is not None:
                    source = _table(_call(_call(out, "GetTool"), "GetAttrs"), "Source.GetAttrs")
                    oa = _table(_call(out, "GetAttrs"), "Output.GetAttrs")
                    edges.append({"source": _text(source.get("TOOLS_Name"), "source name"),
                                  "output": _text(oa.get("OUTS_ID"), "OUTS_ID"),
                                  "target": name, "input": input_id})
            parameters = {key: _primitive(_call(tool, "GetInput", key, ranges["render_start"])) for key in PARAMETERS.get(kind, ())}
            node = {"name": name, "type": kind, "pass_through": bool(attr.get("TOOLB_PassThrough")),
                    "parameters": parameters, "expressions": expressions}
            if kind == "BezierSpline":
                node["keyframes"] = _primitive(_table(_call(tool, "GetKeyFrames"), "GetKeyFrames"))
            nodes.append(node)
        nodes.sort(key=lambda n: n["name"])
        edges.sort(key=lambda e: (e["target"], e["input"], e["source"], e["output"]))
        if len({n["name"] for n in nodes}) != len(nodes):
            raise UnexpectedAPIResponseError("Fusion tool names are not unique.")
        # Catch edits during enumeration, including changes to parameters that
        # aren't part of the compact model-facing parameter list above.
        current_tools = _table(_call(comp, "GetToolList", False), "GetToolList")
        if settings != _copy_settings(comp, current_tools):
            raise StaleSnapshotError("Fusion changed while its graph was being read.")
        final_attrs = _call(comp, "GetAttrs")
        if any(final_attrs[native] != ranges[key] for key, native in (("render_start", "COMPN_RenderStart"), ("render_end", "COMPN_RenderEnd"), ("global_start", "COMPN_GlobalStart"), ("global_end", "COMPN_GlobalEnd"))):
            raise StaleSnapshotError("Fusion frame ranges changed during the read.")
        self._comp(clip_id, comp_name)
        graph = {"schema_version": 1, "project_id": self.project_id, "timeline_id": self.timeline_id,
                 "clip_id": clip_id, "comp_name": comp_name, **ranges, "nodes": nodes, "connections": edges,
                 "settings_hash": _digest(settings)}
        graph["graph_id"] = "fusion-v1-" + _digest(graph)
        return graph


class FusionService(FusionAdapter):
    def __init__(self, adapter, project_id: str, timeline_id: str, journal):
        super().__init__(adapter, project_id, timeline_id)
        self.journal = journal

    def _editable(self, clip_id):
        timeline, item, index = self._target(clip_id)
        state = self._adapter.read_timeline(self.timeline_id)
        if state.project.id != self.project_id:
            raise ProjectScopeError("Project changed during the Fusion read.")
        if not calibration.is_calibrated(state.connection.resolve_version):
            raise UncalibratedBuildError(f"Fusion writes are calibrated only for Resolve {calibration.describe()}.")
        if not state.timeline.name.startswith("AI_BRIDGE_TEST_"):
            raise InvalidOperationError("Prototype Fusion writes require an AI_BRIDGE_TEST_ timeline.")
        track = next(t for t in state.timeline.tracks if t.type == "video" and t.index == index)
        if track.locked or not track.enabled:
            raise InvalidOperationError("Fusion target track is locked or disabled.")
        return state, item

    def _mutate(self, clip_id, obj, method, *args):
        self._editable(clip_id)
        return _write(obj, method, *args)

    def _require_loaded(self, clip_id, comp_name):
        self._editable(clip_id)
        _, timeline = self._adapter._context()
        current_item = _call(timeline, "GetCurrentVideoItem")
        if _call(self._adapter._resolve, "GetCurrentPage") != "fusion" or current_item is None or _id(current_item) != clip_id:
            raise InvalidOperationError("Prepare the requested clip on the Fusion page before writing its graph.")
        current = _call(_call(self._adapter._resolve, "Fusion"), "GetCurrentComp")
        requested = self._comp(clip_id, comp_name)
        if current is None or _call(current, "GetAttrs").get("COMPS_Name") != _call(requested, "GetAttrs").get("COMPS_Name"):
            raise InvalidOperationError("The requested Fusion composition is not loaded in Resolve.")

    def prepare_composition(self, clip_id: str, comp_name: str, *, idempotency_key: str) -> dict:
        """Explicitly position the test clip and load Fusion; returns restore data.

        Resolve can retain a correct detached graph without applying its edits to
        timeline output. Loading the clip before mutation is required on 21.0.0.48.
        This method changes the visible page/playhead and never starts playback.
        """
        self._editable(clip_id)
        def action(operation_id):
            state, item = self._editable(clip_id)
            self._comp(clip_id, comp_name)
            _, timeline = self._adapter._context()
            previous = {"page": _text(_call(self._adapter._resolve, "GetCurrentPage"), "GetCurrentPage"),
                        "timecode": _text(_call(timeline, "GetCurrentTimecode"), "GetCurrentTimecode")}
            # SetCurrentTimecode is not supported on the Fusion page. Resolve's
            # UI changes may finish after the native call acknowledges them.
            if self._mutate(clip_id, self._adapter._resolve, "OpenPage", "edit") is not True:
                raise VerificationError("Resolve did not open Edit to position the clip.")
            _await_value(lambda: _call(self._adapter._resolve, "GetCurrentPage"), "edit", "Resolve page")
            frame = _integer(_call(item, "GetStart", False), "GetStart")
            timecode = frames_to_timecode(frame, state.timeline.frame_rate)
            if self._mutate(clip_id, timeline, "SetCurrentTimecode", timecode) is not True:
                raise VerificationError("Resolve did not position the requested Fusion clip.")
            _await_value(lambda: _call(timeline, "GetCurrentTimecode"), timecode, "Timeline playhead")
            if self._mutate(clip_id, self._adapter._resolve, "OpenPage", "fusion") is not True:
                raise VerificationError("Resolve did not open Fusion.")
            _await_value(lambda: _call(self._adapter._resolve, "GetCurrentPage"), "fusion", "Resolve page")
            if self._mutate(clip_id, item, "LoadFusionCompByName", comp_name) is None:
                raise VerificationError("Resolve did not load the requested composition.")
            self._require_loaded(clip_id, comp_name)
            return {"project_id": self.project_id, "timeline_id": self.timeline_id, **previous}
        return self.journal.execute("fusion_prepare_composition", {"project_id": self.project_id,
            "timeline_id": self.timeline_id, "clip_id": clip_id, "comp_name": comp_name}, idempotency_key, action)

    def restore_view(self, context: dict, *, idempotency_key: str) -> dict:
        """Restore the saved page/playhead while the scoped timeline is active."""
        if not isinstance(context, dict) or (context.get("project_id"), context.get("timeline_id")) != (self.project_id, self.timeline_id):
            raise ProjectScopeError("Fusion view context belongs to another scope.")
        if context.get("page") not in {"media", "photo", "cut", "edit", "fusion", "color", "fairlight", "deliver"}:
            raise InvalidOperationError("Unsupported saved Resolve page.")
        from .timecode import timecode_to_frames
        def action(operation_id):
            state = self._adapter.read_timeline(self.timeline_id)
            if state.project.id != self.project_id:
                raise ProjectScopeError("Project changed before restoring the Fusion view.")
            timecode_to_frames(context.get("timecode"), state.timeline.frame_rate)
            _, timeline = self._adapter._context()
            def guarded_write(obj, method, *args):
                current = self._adapter.read_timeline(self.timeline_id)
                if current.project.id != self.project_id:
                    raise ProjectScopeError("Project changed while restoring the Fusion view.")
                if _write(obj, method, *args) is not True:
                    raise VerificationError(f"Resolve declined {method} while restoring the view.")
            guarded_write(self._adapter._resolve, "OpenPage", "edit")
            _await_value(lambda: _call(self._adapter._resolve, "GetCurrentPage"), "edit", "Resolve page")
            guarded_write(timeline, "SetCurrentTimecode", context["timecode"])
            _await_value(lambda: _call(timeline, "GetCurrentTimecode"), context["timecode"], "Timeline playhead")
            if context["page"] != "edit":
                guarded_write(self._adapter._resolve, "OpenPage", context["page"])
                _await_value(lambda: _call(self._adapter._resolve, "GetCurrentPage"), context["page"], "Resolve page")
            return context
        return self.journal.execute("fusion_restore_view", context, idempotency_key, action)

    @staticmethod
    def _base(graph):
        image_nodes = [node for node in graph["nodes"] if node["type"] != "AudioDisplay"]
        by_kind = {node["type"]: node["name"] for node in image_nodes}
        if len(image_nodes) != 2 or set(by_kind) != {"MediaIn", "MediaOut"}:
            raise InvalidOperationError("Lower thirds require a simple MediaIn → MediaOut composition.")
        expected = {(by_kind["MediaIn"], "Output", by_kind["MediaOut"], "Input")}
        audio_nodes = [node for node in graph["nodes"] if node["type"] == "AudioDisplay"]
        # Loading a fresh Resolve composition creates these hidden waveform
        # helpers. Preserve the observed host topology, including their settings.
        if audio_nodes:
            if len(audio_nodes) != 2 or {node["name"] for node in audio_nodes} != {"Left", "Right"}:
                raise InvalidOperationError("Unsupported Fusion audio display nodes.")
            expected |= {(side, "Data", by_kind["MediaIn"], side + "Audio") for side in ("Left", "Right")}
        edges = {(e["source"], e["output"], e["target"], e["input"]) for e in graph["connections"]}
        if edges != expected or any(n["expressions"] or n["pass_through"] for n in graph["nodes"]):
            raise InvalidOperationError("Base graph has unsupported connections, expressions, or bypassed nodes.")
        return by_kind

    def create_composition(self, clip_id: str, *, expected_snapshot_id: str, idempotency_key: str) -> dict:
        self._editable(clip_id)
        def action(operation_id):
            state, item = self._editable(clip_id)
            require_snapshot(state, expected_snapshot_id)
            if self.list_compositions(clip_id):
                raise InvalidOperationError("Create composition requires a clip with no existing Fusion composition.")
            comp = self._mutate(clip_id, item, "AddFusionComp")
            names = self.list_compositions(clip_id)
            if comp is None or len(names) != 1:
                raise VerificationError("AddFusionComp did not produce exactly one composition.")
            graph = self.read_graph(clip_id, names[0])
            self._base(graph)
            require_snapshot(self._editable(clip_id)[0], state.snapshot_id)
            return {"graph": graph}
        return self.journal.execute("fusion_create_composition", {"project_id": self.project_id, "timeline_id": self.timeline_id,
            "clip_id": clip_id, "expected_snapshot_id": expected_snapshot_id}, idempotency_key, action)

    def _validate(self, plan, graph):
        if not isinstance(plan.text, str) or not 1 <= len(plan.text.strip()) <= 160 or len(plan.text) > 160 or plan.text.count("\n") > 1 or any(ord(c) < 32 and c != "\n" for c in plan.text):
            raise InvalidOperationError("Text must contain 1–160 characters and at most two lines.")
        _number(plan.x, "x", 0.1, 0.9)
        _number(plan.y, "y", 0.1, 0.35)
        _number(plan.size, "size", 0.02, 0.12)
        length = graph["render_end"] - graph["render_start"] + 1
        if type(plan.fade_frames) is not int or not 1 <= plan.fade_frames <= 120 or 2 * plan.fade_frames >= length - 1:
            raise InvalidOperationError("Fade needs 1–120 frames and room for an opaque hold between the fades.")
        self._base(graph)

    def plan_lower_third(self, *, clip_id: str, comp_name: str, expected_snapshot_id: str,
                         expected_graph_id: str, text: str, x: float = 0.5, y: float = 0.18,
                         size: float = 0.065, fade_frames: int = 12) -> LowerThirdPlan:
        state, _ = self._editable(clip_id)
        require_snapshot(state, expected_snapshot_id)
        graph = self.read_graph(clip_id, comp_name)
        if graph["graph_id"] != expected_graph_id:
            raise StaleSnapshotError("Fusion graph changed since planning.")
        plan = LowerThirdPlan(self.project_id, self.timeline_id, clip_id, comp_name, expected_snapshot_id,
                              expected_graph_id, text, x, y, size, fade_frames)
        self._validate(plan, graph)
        return plan

    def execute_lower_third(self, plan: LowerThirdPlan, *, idempotency_key: str) -> dict:
        if not isinstance(plan, LowerThirdPlan) or (plan.project_id, plan.timeline_id) != (self.project_id, self.timeline_id):
            raise ProjectScopeError("Lower-third plan belongs to a different scope.")
        self._editable(plan.clip_id)
        def action(operation_id):
            self._require_loaded(plan.clip_id, plan.comp_name)
            # Revalidate even a deserialized or manually constructed plan.
            self.plan_lower_third(**{key: value for key, value in plan.to_dict().items() if key not in ("project_id", "timeline_id")})
            before = self.read_graph(plan.clip_id, plan.comp_name)
            comp = self._comp(plan.clip_id, plan.comp_name)
            if _call(comp, "IsLocked"):
                raise InvalidOperationError("Fusion composition is already locked.")
            base = self._base(before)
            tools = { _call(t, "GetAttrs")["TOOLS_Name"]: t for t in _call(comp, "GetToolList", False).values() }
            original_settings = _copy_settings(comp, _call(comp, "GetToolList", False))
            start, end = before["render_start"], before["render_end"]
            keys = {start: 0.0, start + plan.fade_frames: 1.0, end - plan.fade_frames: 1.0, end: 0.0}
            mutate = lambda obj, method, *args: self._mutate(plan.clip_id, obj, method, *args)
            undo_started = False
            try:
                # Keep Resolve's host notifications enabled while modifying the
                # loaded comp. Lock/Unlock can leave timeline output unrefreshed.
                if self.read_graph(plan.clip_id, plan.comp_name)["graph_id"] != plan.expected_graph_id:
                    raise StaleSnapshotError("Fusion changed immediately before the write.")
                mutate(comp, "StartUndo", "AI Bridge: animated lower third")
                undo_started = True
                text_tool = mutate(comp, "AddTool", "TextPlus", False, 2, 0)
                merge = mutate(comp, "AddTool", "Merge", False, 2, 1)
                if text_tool is None or merge is None:
                    raise VerificationError("Fusion did not create Text+ and Merge.")
                for tool, name in ((text_tool, TEXT_NAME), (merge, MERGE_NAME)):
                    mutate(tool, "SetAttrs", {"TOOLS_Name": name, "TOOLB_NameSet": True})
                    if _call(tool, "GetAttrs").get("TOOLS_Name") != name:
                        raise VerificationError("Fusion did not assign the requested tool name.")
                desired = {"StyledText": plan.text, "Font": "Arial", "Style": "Bold", "Size": plan.size,
                           "Center": {1: plan.x, 2: plan.y}}
                for key, value in desired.items():
                    mutate(text_tool, "SetInput", key, value)
                for tool, input_id, source in ((merge, "Background", tools[base["MediaIn"]]),
                                                (merge, "Foreground", text_tool),
                                                (tools[base["MediaOut"]], "Input", merge)):
                    if mutate(tool, "ConnectInput", input_id, source) is not True:
                        raise VerificationError("Fusion declined a graph connection.")
                if mutate(merge, "AddModifier", "Blend", "BezierSpline") is not True:
                    raise VerificationError("Fusion did not attach the opacity animation.")
                blend = next(i for i in _call(merge, "GetInputList").values() if _call(i, "GetAttrs")["INPS_ID"] == "Blend")
                output = _call(blend, "GetConnectedOutput")
                if output is None:
                    raise VerificationError("Opacity modifier is not connected.")
                spline = _call(output, "GetTool")
                mutate(spline, "SetAttrs", {"TOOLS_Name": SPLINE_NAME, "TOOLB_NameSet": True})
                mutate(spline, "SetKeyFrames", {frame: {1: value} for frame, value in keys.items()}, True)
            finally:
                # Keep an Undo event even after a partial failure. EndUndo(False)
                # discards history; it does NOT roll changes back.
                if undo_started:
                    _write(comp, "EndUndo", True)
            after = self.read_graph(plan.clip_id, plan.comp_name)
            self._verify(plan, before, after, comp, original_settings, keys)
            require_snapshot(self._editable(plan.clip_id)[0], plan.expected_snapshot_id)
            return {"before_graph_id": before["graph_id"], "graph": after,
                    "keyframes": [{"frame": f, "value": v} for f, v in keys.items()], "timeline_unchanged": True}
        return self.journal.execute("fusion_lower_third", plan.to_dict(), idempotency_key, action)

    def _verify(self, plan, before, after, comp, original_settings, keys):
        nodes = {n["name"]: n for n in after["nodes"]}
        original_names = {n["name"] for n in before["nodes"]}
        if set(nodes) != original_names | {TEXT_NAME, MERGE_NAME, SPLINE_NAME} or any(nodes[name]["type"] != kind for name, kind in ((TEXT_NAME, "TextPlus"), (MERGE_NAME, "Merge"), (SPLINE_NAME, "BezierSpline"))):
            raise VerificationError("Unexpected Fusion nodes after lower-third creation.")
        text = nodes[TEXT_NAME]["parameters"]
        if text["StyledText"] != plan.text or text["Font"] != "Arial" or text["Style"] != "Bold" or not math.isclose(text["Size"], plan.size, abs_tol=1e-6) or any(not math.isclose(text["Center"].get(str(i), -1), v, abs_tol=1e-6) for i, v in ((1, plan.x), (2, plan.y))):
            raise VerificationError("Text+ text, font, position, or size did not match the plan.")
        base = self._base(before)
        expected_edges = {(base["MediaIn"], "Output", MERGE_NAME, "Background"), (TEXT_NAME, "Output", MERGE_NAME, "Foreground"),
                          (MERGE_NAME, "Output", base["MediaOut"], "Input"), (SPLINE_NAME, "Value", MERGE_NAME, "Blend"),
                          # Resolve 21 exposes a second UI control for the same
                          # Merge opacity. AddModifier wires this alias too.
                          (SPLINE_NAME, "Value", MERGE_NAME, "BlendClone")}
        expected_edges |= {(e["source"], e["output"], e["target"], e["input"])
                           for e in before["connections"] if e["target"] == base["MediaIn"]}
        if {(e["source"], e["output"], e["target"], e["input"]) for e in after["connections"]} != expected_edges:
            raise VerificationError("Fusion graph wiring did not match the recipe.")
        actual_keys = nodes[SPLINE_NAME]["keyframes"]
        if set(actual_keys) != {str(f) for f in keys} or any(not math.isclose(actual_keys[str(f)]["1"], v, abs_tol=1e-6) for f, v in keys.items()):
            raise VerificationError("Opacity animation keyframes did not match the plan.")
        tools = _call(comp, "GetToolList", False)
        settings = _copy_settings(comp, tools)
        for name in original_names:
            expected = original_settings["Tools"][name]
            if name == base["MediaOut"]:
                expected = json.loads(json.dumps(expected))
                expected["Inputs"]["Input"] = {"__ctor": "Input", "SourceOp": MERGE_NAME, "Source": "Output"}
            if settings["Tools"].get(name) != expected:
                raise VerificationError("An existing Fusion tool changed beyond the planned output connection.")
        merge = next(t for t in tools.values() if _call(t, "GetAttrs")["TOOLS_Name"] == MERGE_NAME)
        for frame, value in keys.items():
            actual = _call(merge, "GetInput", "Blend", frame)
            if not isinstance(actual, (int, float)) or not math.isclose(actual, value, abs_tol=1e-6):
                raise VerificationError("Evaluated opacity does not match a keyframe.")
        if plan.fade_frames > 1:
            for frame in (before["render_start"] + plan.fade_frames / 2, before["render_end"] - plan.fade_frames / 2):
                if not 0 < _call(merge, "GetInput", "Blend", frame) < 1:
                    raise VerificationError("Opacity is not interpolating between keyframes.")
