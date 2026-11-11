"""Bounded Fusion recipe: a scrolling MIDI piano roll over a test clip.

Notes come from a Standard MIDI File (resolve_bridge.midi, a port of the parser
ae-scripts verified against MIDI-HERO). Each note is one Rectangle mask. Notes
are grouped into chunks one frame-width of music wide; a chunk's masks chain
into one Background, and one Merge per chunk scrolls it with a two-key linear
spline, so a note's left edge reaches the playhead line on the exact (sub)frame
of its note-on per the tempo map. Everything left of the playhead is brightened
by one masked BrightnessContrast, so a bar lights at its note-on and stays lit as
it passes. Optional per-bar timeline markers ride in the same journal operation.

Node count is about one per note plus four per chunk plus seven, capped at
MAX_NODES; over the cap the plan fails with the counts and what to narrow.
Like the lower third: plan, then journaled execute; test timelines only; the
existing graph must be a fresh MediaIn -> MediaOut; full readback afterwards.
"""

from dataclasses import asdict, dataclass, replace
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

from . import midi
from ._api import _call, _integer, _write
from .errors import InvalidOperationError, ProjectScopeError, StaleSnapshotError, UnexpectedAPIResponseError, VerificationError
from .fusion import FusionService, _copy_settings, _number
from .models import MarkerState
from .snapshot import require_snapshot

# Caps. A Fusion comp with thousands of nodes is unusable, and every graph read
# walks every input of every node over the scripting bridge (inferred cost:
# tens of native calls per node), so the recipe stays in the low hundreds.
MAX_NODES = 256
MAX_NOTE_RECTS = 200
MAX_CHUNKS = 12
MAX_ROWS = 88
MAX_BAR_MARKERS = 64
MAX_MIDI_BYTES = 2 * 1024 * 1024
FIXED_NODES = 7   # canvas, lit mask, lit, overlay merge, playhead mask, playhead, playhead merge
NODES_PER_CHUNK = 4  # background, merge, XY path, scroll spline

# Look. Fusion coordinates: (0, 0) bottom-left, (1, 1) top-right.
ROLL_BOTTOM, ROLL_TOP = 0.12, 0.88
MIN_RANGE = 12          # pad a narrow melody to an octave of rows, as the AE script does
ROW_FILL = 0.8          # bar height as a fraction of its row
GAP = 0.002             # trimmed off a note's right end (frame widths), so repeated notes separate
MIN_WIDTH = 0.004       # shortest drawn bar (frame widths)
PLAYHEAD_WIDTH = 0.003
ROLL_COLOR = (0.16, 0.55, 0.68)
PLAYHEAD_COLOR = (1.0, 1.0, 1.0)
LIT_GAIN = 2.0
MARKER_COLOR = "Purple"
# Assumption to confirm in the live smoke test: Fusion sizes a mask's Width and
# Height in units of the image WIDTH (equal values draw a square), while Center
# is normalized per axis. Bar heights are converted with the frame aspect. If the
# live frame shows squashed or overlapping rows, set this to "per_axis".
MASK_SIZE_UNIT = "image_width"

PREFIX = "AI_Roll_"
CANVAS, LIT_MASK, LIT, OVER = PREFIX + "Canvas", PREFIX + "LitMask", PREFIX + "Lit", PREFIX + "Over"
PLAYHEAD_MASK, PLAYHEAD, PLAYHEAD_MERGE = PREFIX + "PlayheadMask", PREFIX + "Playhead", PREFIX + "PlayheadMerge"
OUTPUT_IDS = {"RectangleMask": "Mask", "BezierSpline": "Value", "XYPath": "Value"}


def _note_name(k, j):
    return f"{PREFIX}Note_{k:02d}_{j:03d}"


def _chunk_names(k):
    return {kind: f"{PREFIX}{kind}_{k:02d}" for kind in ("Chunk", "Merge", "Path", "Scroll")}


def _round_half_up(value):
    return math.floor(value + 0.5)


@dataclass(frozen=True)
class MidiRollPlan:
    project_id: str
    timeline_id: str
    clip_id: str
    comp_name: str
    expected_snapshot_id: str
    expected_graph_id: str
    midi_path: str
    midi_sha256: str
    track: int | None
    channel: int | None
    skip_drums: bool
    start_seconds: float
    end_seconds: float | None
    min_pitch: int | None
    max_pitch: int | None
    seconds_visible: float
    playhead_x: float
    bar_markers: bool
    layout_sha256: str

    def to_dict(self):
        return asdict(self)


class RollTooLargeError(InvalidOperationError):
    code = "roll_too_large"


# ---------------------------------------------------------------- pure layout

def read_midi_file(path: str) -> tuple[bytes, str]:
    """Bounded read of one .mid/.midi file. Path policy (allowlists) is the caller's."""
    if not isinstance(path, str) or not path:
        raise InvalidOperationError("midi_path must be a nonempty path string.")
    file = Path(path).expanduser()
    if not file.is_absolute():
        raise InvalidOperationError("midi_path must be absolute.")
    if file.suffix.lower() not in (".mid", ".midi"):
        raise InvalidOperationError("midi_path must name a .mid or .midi file.")
    if not file.is_file():
        raise InvalidOperationError("midi_path is not an existing regular file.")
    if file.stat().st_size > MAX_MIDI_BYTES:
        raise InvalidOperationError(f"MIDI file is larger than {MAX_MIDI_BYTES} bytes.")
    data = file.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def parse_midi(data: bytes):
    try:
        return midi.parse(data)
    except midi.MidiError as exc:
        raise InvalidOperationError(f"MIDI file rejected: {exc}") from exc


def build_layout(song, *, fps: Fraction, render_start: int, render_end: int, frame_width: int, frame_height: int,
                 clip_start_frame: int, track=None, channel=None, skip_drums=True, start_seconds=0.0,
                 end_seconds=None, min_pitch=None, max_pitch=None, seconds_visible=6.0, playhead_x=0.25,
                 bar_markers=False) -> dict:
    """Everything the recipe will create, as plain data. No Resolve calls.

    Song time tau at comp frame f is start_seconds + (f - render_start) / fps.
    Chunk k holds song time [start + k*S, start + (k+1)*S), S = seconds_visible,
    drawn on a frame-sized Background in local x = (t - start)/S - k. Its Merge
    center x is playhead_x + 0.5 + k - (f - render_start) / (fps*S), linear in
    f, so a note's left edge sits at playhead_x exactly when tau equals its onset.
    """
    if type(fps) is not Fraction or fps <= 0:
        raise InvalidOperationError("Timeline frame rate must be a positive exact rate.")
    for value, label in ((render_start, "render_start"), (render_end, "render_end"), (clip_start_frame, "clip start"),
                         (frame_width, "frame width"), (frame_height, "frame height")):
        if type(value) is not int:
            raise UnexpectedAPIResponseError(f"{label} must be an integer.")
    if render_end < render_start or frame_width <= 0 or frame_height <= 0:
        raise UnexpectedAPIResponseError("Composition range or frame size is invalid.")
    if render_end - render_start < 23:
        raise InvalidOperationError("The piano roll needs a clip of at least 24 frames.")
    if track is not None and (type(track) is not int or not 1 <= track <= len(song.tracks)):
        raise InvalidOperationError(f"track must be 1-{len(song.tracks)} (1-based, as read_midi_file lists them).")
    if channel is not None and (type(channel) is not int or not 1 <= channel <= 16):
        raise InvalidOperationError("channel must be 1-16.")
    if type(skip_drums) is not bool or type(bar_markers) is not bool:
        raise InvalidOperationError("skip_drums and bar_markers must be booleans.")
    _number(start_seconds, "start_seconds", 0, 3600)
    _number(seconds_visible, "seconds_visible", 2, 20)
    _number(playhead_x, "playhead_x", 0.1, 0.9)
    for value, label in ((min_pitch, "min_pitch"), (max_pitch, "max_pitch")):
        if value is not None and (type(value) is not int or not 0 <= value <= 127):
            raise InvalidOperationError(f"{label} must be an integer 0-127.")

    S, P = float(seconds_visible), float(playhead_x)
    t0 = float(start_seconds)
    frames = render_end - render_start + 1
    clip_seconds = float(Fraction(frames) / fps)
    visible_end = t0 + clip_seconds + (1 - P) * S  # the last song time that reaches the screen
    if end_seconds is None:
        t_end = visible_end
    else:
        _number(end_seconds, "end_seconds", 0, 3600)
        if end_seconds <= t0:
            raise InvalidOperationError("end_seconds must be after start_seconds.")
        t_end = min(float(end_seconds), visible_end)

    lo_filter = 0 if min_pitch is None else min_pitch
    hi_filter = 127 if max_pitch is None else max_pitch
    if lo_filter > hi_filter:
        raise InvalidOperationError("min_pitch must not exceed max_pitch.")
    selected = midi.select_notes(song, -1 if track is None else track - 1, channel=-1 if channel is None else channel - 1,
                                 min_pitch=lo_filter, max_pitch=hi_filter, skip_drums=skip_drums)
    notes = [n for n in selected if t0 <= n.time < t_end]
    if not notes:
        raise InvalidOperationError("No notes match the track, channel, pitch and time filters inside the clip's window.")

    lo = min(n.pitch for n in notes) if min_pitch is None else min_pitch
    hi = max(n.pitch for n in notes) if max_pitch is None else max_pitch
    if min_pitch is None and max_pitch is None and hi - lo + 1 < MIN_RANGE:
        extra = MIN_RANGE - (hi - lo + 1)
        lo, hi = lo - extra // 2, hi + extra - extra // 2
        if lo < 0:
            lo, hi = 0, hi - lo
        if hi > 127:
            lo, hi = lo - (hi - 127), 127
    rows = hi - lo + 1
    if rows > MAX_ROWS:
        raise RollTooLargeError(f"Pitch range spans {rows} rows; the cap is {MAX_ROWS}. Set min_pitch/max_pitch.")
    row_h = (ROLL_TOP - ROLL_BOTTOM) / rows
    mask_height = row_h * ROW_FILL * (frame_height / frame_width if MASK_SIZE_UNIT == "image_width" else 1)

    chunks = {}
    last_visible = (visible_end - t0) / S  # roll x that reaches the right edge on the clip's last frame
    for note in notes:
        left = (note.time - t0) / S
        # A long held note is cut where it can no longer reach the screen, so it
        # doesn't fill chunks that never scroll into view.
        right = min(left + max(MIN_WIDTH, note.duration / S - GAP), max(last_visible, left + MIN_WIDTH))
        y = ROLL_BOTTOM + (note.pitch - lo + 0.5) * row_h
        k = math.floor(left)
        while k < right:
            piece_l, piece_r = max(left, k) - k, min(right, k + 1) - k
            if piece_r - piece_l > 1e-9:
                chunks.setdefault(k, []).append({"x": (piece_l + piece_r) / 2, "y": y, "width": piece_r - piece_l,
                                                 "height": mask_height, "pitch": note.pitch,
                                                 "onset": note.time if piece_l == left - k else None})
            k += 1
    rects = sum(len(v) for v in chunks.values())
    nodes = FIXED_NODES + NODES_PER_CHUNK * len(chunks) + rects
    if rects > MAX_NOTE_RECTS or len(chunks) > MAX_CHUNKS or nodes > MAX_NODES:
        raise RollTooLargeError(
            f"This selection needs {nodes} Fusion nodes ({rects} note rectangles from {len(notes)} notes in "
            f"{len(chunks)} chunks); caps are {MAX_NODES} nodes, {MAX_NOTE_RECTS} rectangles, {MAX_CHUNKS} chunks. "
            "Narrow the time range (start_seconds/end_seconds or a shorter clip), pick one track or channel, "
            "or narrow the pitch range.")

    def scroll_x(k, frame):
        return P + 0.5 + k - (frame - render_start) / (float(fps) * S)

    def onset_frame(seconds):
        return render_start + (seconds - t0) * float(fps)

    layout_chunks = []
    for k in sorted(chunks):
        names = _chunk_names(k)
        layout_chunks.append({"index": k, **{key.lower(): name for key, name in names.items()},
                              "notes": [{"name": _note_name(k, j), **rect} for j, rect in enumerate(chunks[k])],
                              "keys": {render_start: scroll_x(k, render_start), render_end: scroll_x(k, render_end)}})

    markers = []
    if bar_markers:
        # Bars inside the clip that start before the music ends; the meter
        # otherwise runs on forever past the last note.
        clip_end_song = min(t0 + clip_seconds, song.end_time)
        for line in midi.beat_lines(song, clip_end_song):
            if not line["is_bar"] or not t0 <= line["time"] < clip_end_song:
                continue
            offset = _round_half_up((line["time"] - t0) * float(fps))
            if offset >= frames:
                continue
            markers.append({"timeline_frame": clip_start_frame + offset, "bar": line["bar"], "seconds": line["time"],
                            "name": f"Bar {line['bar']}", "note": f"{line['numerator']}/{line['denominator']} at {line['time']:.6f} s"})
        if len(markers) > MAX_BAR_MARKERS:
            raise RollTooLargeError(f"{len(markers)} bar markers exceed the cap of {MAX_BAR_MARKERS}; shorten the clip or turn bar_markers off.")
        if len({m["timeline_frame"] for m in markers}) != len(markers):
            raise InvalidOperationError("Two bars round to the same timeline frame; turn bar_markers off.")

    layout = {"frame_width": frame_width, "frame_height": frame_height, "fps": f"{fps.numerator}/{fps.denominator}",
              "render_start": render_start, "render_end": render_end, "start_seconds": t0, "end_seconds": t_end,
              "seconds_visible": S, "playhead_x": P, "min_pitch": lo, "max_pitch": hi, "rows": rows,
              "note_count": len(notes), "rect_count": rects, "chunk_count": len(chunks), "node_count": nodes,
              "chunks": layout_chunks, "bar_markers": markers,
              "first_onsets": [{"seconds": n.time, "comp_frame": onset_frame(n.time), "pitch": n.pitch,
                                "name": midi.note_name(n.pitch)} for n in notes[:8]]}
    return layout


def layout_digest(layout: dict) -> str:
    return hashlib.sha256(json.dumps(layout, sort_keys=True, separators=(",", ":"), allow_nan=False,
                                     default=str).encode()).hexdigest()


def expected_graph(layout: dict, base: dict) -> tuple[dict, set]:
    """Planned node name -> type, and the full planned edge set (source, output, target, input)."""
    types = {CANVAS: "Background", LIT_MASK: "RectangleMask", LIT: "BrightnessContrast", OVER: "Merge",
             PLAYHEAD_MASK: "RectangleMask", PLAYHEAD: "Background", PLAYHEAD_MERGE: "Merge"}
    edges = set()
    previous = CANVAS
    for chunk in layout["chunks"]:
        types.update({chunk["chunk"]: "Background", chunk["merge"]: "Merge", chunk["path"]: "XYPath", chunk["scroll"]: "BezierSpline"})
        names = [note["name"] for note in chunk["notes"]]
        types.update({name: "RectangleMask" for name in names})
        edges |= {(a, "Mask", b, "EffectMask") for a, b in zip(names, names[1:])}
        edges |= {(names[-1], "Mask", chunk["chunk"], "EffectMask"),
                  (previous, "Output", chunk["merge"], "Background"), (chunk["chunk"], "Output", chunk["merge"], "Foreground"),
                  (chunk["path"], "Value", chunk["merge"], "Center"), (chunk["scroll"], "Value", chunk["path"], "X")}
        previous = chunk["merge"]
    edges |= {(previous, "Output", LIT, "Input"), (LIT_MASK, "Mask", LIT, "EffectMask"),
              (base["MediaIn"], "Output", OVER, "Background"), (LIT, "Output", OVER, "Foreground"),
              (PLAYHEAD_MASK, "Mask", PLAYHEAD, "EffectMask"),
              (OVER, "Output", PLAYHEAD_MERGE, "Background"), (PLAYHEAD, "Output", PLAYHEAD_MERGE, "Foreground"),
              (PLAYHEAD_MERGE, "Output", base["MediaOut"], "Input")}
    return types, edges


def _linear_keys(keys: dict) -> dict:
    """Two keys with handles on the straight line between them: exactly linear in time."""
    (f0, v0), (f1, v1) = sorted(keys.items())
    third_f, third_v = (f1 - f0) / 3, (v1 - v0) / 3
    return {f0: {1: v0, "RH": {1: f0 + third_f, 2: v0 + third_v}, "Flags": {"Linear": True}},
            f1: {1: v1, "LH": {1: f1 - third_f, 2: v1 - third_v}, "Flags": {"Linear": True}}}


def _point(value, label):
    if not isinstance(value, dict):
        raise VerificationError(f"{label} did not read back as a point.")
    x, y = value.get(1, value.get("1")), value.get(2, value.get("2"))
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (x, y)):
        raise VerificationError(f"{label} did not read back as a finite point.")
    return x, y


# ---------------------------------------------------------------- service

class MidiRollService(FusionService):
    """Plan/execute for the piano roll; inherits scope, loading and graph reads."""

    def _frame_size(self, comp):
        size = []
        for key in ("Comp.FrameFormat.Width", "Comp.FrameFormat.Height"):
            value = _call(comp, "GetPrefs", key)
            size.append(_integer(value, key))
        if not all(16 <= v <= 16384 for v in size):
            raise UnexpectedAPIResponseError("Composition frame format is outside 16-16384 pixels.")
        return tuple(size)

    def _layout(self, state, item, graph, song, params):
        comp = self._comp(params["clip_id"], params["comp_name"])
        width, height = self._frame_size(comp)
        rate = state.timeline.frame_rate
        return build_layout(song, fps=Fraction(rate.numerator, rate.denominator), render_start=graph["render_start"],
                            render_end=graph["render_end"], frame_width=width, frame_height=height,
                            clip_start_frame=_integer(_call(item, "GetStart", False), "GetStart"),
                            **{key: params[key] for key in ("track", "channel", "skip_drums", "start_seconds", "end_seconds",
                                                             "min_pitch", "max_pitch", "seconds_visible", "playhead_x", "bar_markers")})

    def _check_markers(self, state, layout):
        occupied = {marker.offset_frame for marker in state.timeline.markers}
        for marker in layout["bar_markers"]:
            offset = marker["timeline_frame"] - state.timeline.start_frame
            if offset < 0 or marker["timeline_frame"] >= state.timeline.end_frame or offset in occupied:
                raise InvalidOperationError(f"Bar marker frame {marker['timeline_frame']} is outside the timeline or already has a marker.")

    def plan_midi_roll(self, *, clip_id: str, comp_name: str, expected_snapshot_id: str, expected_graph_id: str,
                       midi_path: str, track: int | None = None, channel: int | None = None, skip_drums: bool = True,
                       start_seconds: float = 0.0, end_seconds: float | None = None, min_pitch: int | None = None,
                       max_pitch: int | None = None, seconds_visible: float = 6.0, playhead_x: float = 0.25,
                       bar_markers: bool = False) -> tuple[MidiRollPlan, dict]:
        """Read-only. Returns the frozen plan and its layout summary."""
        state, item = self._editable(clip_id)
        require_snapshot(state, expected_snapshot_id)
        graph = self.read_graph(clip_id, comp_name)
        if graph["graph_id"] != expected_graph_id:
            raise StaleSnapshotError("Fusion graph changed since it was read.")
        self._base(graph)
        data, digest = read_midi_file(midi_path)
        params = dict(clip_id=clip_id, comp_name=comp_name, track=track, channel=channel, skip_drums=skip_drums,
                      start_seconds=start_seconds, end_seconds=end_seconds, min_pitch=min_pitch, max_pitch=max_pitch,
                      seconds_visible=seconds_visible, playhead_x=playhead_x, bar_markers=bar_markers)
        layout = self._layout(state, item, graph, parse_midi(data), params)
        self._check_markers(state, layout)
        plan = MidiRollPlan(self.project_id, self.timeline_id, clip_id, comp_name, expected_snapshot_id, expected_graph_id,
                            midi_path, digest, track, channel, skip_drums, start_seconds, end_seconds, min_pitch, max_pitch,
                            seconds_visible, playhead_x, bar_markers, layout_digest(layout))
        return plan, layout

    def execute_midi_roll(self, plan: MidiRollPlan, *, idempotency_key: str) -> dict:
        if not isinstance(plan, MidiRollPlan) or (plan.project_id, plan.timeline_id) != (self.project_id, self.timeline_id):
            raise ProjectScopeError("MIDI roll plan belongs to a different scope.")
        self._editable(plan.clip_id)

        def action(operation_id):
            self._require_loaded(plan.clip_id, plan.comp_name)
            # Revalidate everything, including a forged or deserialized plan.
            fields = {k: v for k, v in plan.to_dict().items() if k not in ("project_id", "timeline_id", "midi_sha256", "layout_sha256")}
            fresh, layout = self.plan_midi_roll(**fields)
            if fresh.midi_sha256 != plan.midi_sha256:
                raise StaleSnapshotError("The MIDI file changed since planning.")
            if fresh.layout_sha256 != plan.layout_sha256:
                raise StaleSnapshotError("The planned layout no longer matches (frame size, rate or range changed).")
            state, _ = self._editable(plan.clip_id)
            before = self.read_graph(plan.clip_id, plan.comp_name)
            base = self._base(before)
            comp = self._comp(plan.clip_id, plan.comp_name)
            if _call(comp, "IsLocked"):
                raise InvalidOperationError("Fusion composition is already locked.")
            original_settings = _copy_settings(comp, _call(comp, "GetToolList", False))
            tools = {_call(t, "GetAttrs")["TOOLS_Name"]: t for t in _call(comp, "GetToolList", False).values()}
            if self.read_graph(plan.clip_id, plan.comp_name)["graph_id"] != plan.expected_graph_id:
                raise StaleSnapshotError("Fusion changed immediately before the write.")
            undo_started = False
            try:
                _write(comp, "StartUndo", "AI Bridge: MIDI piano roll")
                undo_started = True
                self._build(comp, tools, base, layout, plan.clip_id)
            finally:
                if undo_started:
                    _write(comp, "EndUndo", True)  # keeps a recovery event; does not roll back
            after = self.read_graph(plan.clip_id, plan.comp_name)
            checks = self._verify(layout, before, after, comp, original_settings, base)
            markers = self._add_markers(plan, state, layout, operation_id)
            return {"before_graph_id": before["graph_id"], "graph_id": after["graph_id"],
                    "node_count": len(after["nodes"]), "added_nodes": layout["node_count"],
                    "note_count": layout["note_count"], "rect_count": layout["rect_count"],
                    "chunk_count": layout["chunk_count"], "pitch_range": [layout["min_pitch"], layout["max_pitch"]],
                    "onset_checks": checks, "bar_markers": markers, "visual_verification": "not_performed"}

        return self.journal.execute("fusion_midi_roll", plan.to_dict(), idempotency_key, action)

    # -- build
    def _build(self, comp, tools, base, layout, clip_id):
        def add(kind, name, x=0, y=0):
            tool = _write(comp, "AddTool", kind, False, x, y)
            if tool is None:
                raise VerificationError(f"Fusion did not create {kind}.")
            _write(tool, "SetAttrs", {"TOOLS_Name": name, "TOOLB_NameSet": True})
            if _call(tool, "GetAttrs").get("TOOLS_Name") != name:
                raise VerificationError(f"Fusion did not name the new {kind} {name}.")
            return tool

        def connect(tool, input_id, source):
            if _write(tool, "ConnectInput", input_id, source) is not True:
                raise VerificationError(f"Fusion declined connecting {input_id}.")

        def modifier(tool, input_id, kind, name):
            if _write(tool, "AddModifier", input_id, kind) is not True:
                raise VerificationError(f"Fusion did not attach {kind} to {input_id}.")
            inp = next((i for i in _call(tool, "GetInputList").values() if _call(i, "GetAttrs")["INPS_ID"] == input_id), None)
            output = _call(inp, "GetConnectedOutput") if inp is not None else None
            if output is None:
                raise VerificationError(f"{kind} is not connected to {input_id}.")
            created = _call(output, "GetTool")
            _write(created, "SetAttrs", {"TOOLS_Name": name, "TOOLB_NameSet": True})
            return created

        def color(tool, rgb, alpha=1.0):
            for key, value in zip(("TopLeftRed", "TopLeftGreen", "TopLeftBlue", "TopLeftAlpha"), (*rgb, alpha)):
                _write(tool, "SetInput", key, value)

        def rectangle(name, x, y, width, height, column):
            mask = add("RectangleMask", name, column, 4)
            for key, value in (("Center", {1: x, 2: y}), ("Width", width), ("Height", height)):
                _write(mask, "SetInput", key, value)
            return mask

        # Scope is rechecked before each chunk rather than every native call:
        # a 250-node build would otherwise reread the timeline ~1,500 times.
        self._editable(clip_id)
        canvas = add("Background", CANVAS, 0, 1)
        color(canvas, (0.0, 0.0, 0.0), 0.0)
        previous = canvas
        for column, chunk in enumerate(layout["chunks"], 1):
            self._editable(clip_id)
            mask = None
            for note in chunk["notes"]:
                new = rectangle(note["name"], note["x"], note["y"], note["width"], note["height"], column)
                if mask is not None:
                    connect(new, "EffectMask", mask)
                mask = new
            background = add("Background", chunk["chunk"], column, 2)
            color(background, ROLL_COLOR)
            connect(background, "EffectMask", mask)
            merge = add("Merge", chunk["merge"], column, 1)
            connect(merge, "Background", previous)
            connect(merge, "Foreground", background)
            path = modifier(merge, "Center", "XYPath", chunk["path"])
            _write(path, "SetInput", "Y", 0.5)
            spline = modifier(path, "X", "BezierSpline", chunk["scroll"])
            _write(spline, "SetKeyFrames", _linear_keys(chunk["keys"]), True)
            previous = merge
        self._editable(clip_id)
        end = len(layout["chunks"]) + 1
        P = layout["playhead_x"]
        lit_mask = rectangle(LIT_MASK, P / 2, 0.5, P, 2.0, end)
        lit = add("BrightnessContrast", LIT, end, 1)
        _write(lit, "SetInput", "Gain", LIT_GAIN)
        connect(lit, "Input", previous)
        connect(lit, "EffectMask", lit_mask)
        over = add("Merge", OVER, end + 1, 0)
        connect(over, "Background", tools[base["MediaIn"]])
        connect(over, "Foreground", lit)
        playhead_mask = rectangle(PLAYHEAD_MASK, P, 0.5, PLAYHEAD_WIDTH, 2.0, end + 2)
        playhead = add("Background", PLAYHEAD, end + 2, 1)
        color(playhead, PLAYHEAD_COLOR)
        connect(playhead, "EffectMask", playhead_mask)
        playhead_merge = add("Merge", PLAYHEAD_MERGE, end + 3, 0)
        connect(playhead_merge, "Background", over)
        connect(playhead_merge, "Foreground", playhead)
        self._editable(clip_id)
        connect(tools[base["MediaOut"]], "Input", playhead_merge)

    # -- verify
    def _verify(self, layout, before, after, comp, original_settings, base):
        types, edges = expected_graph(layout, base)
        nodes = {n["name"]: n for n in after["nodes"]}
        original = {n["name"] for n in before["nodes"]}
        if set(nodes) != original | set(types):
            extra, missing = sorted(set(nodes) - original - set(types))[:5], sorted(set(types) - set(nodes))[:5]
            raise VerificationError(f"Unexpected Fusion nodes after the build: extra {extra}, missing {missing}.")
        wrong = [name for name, kind in types.items() if nodes[name]["type"] != kind]
        if wrong:
            raise VerificationError(f"Fusion node types differ from the plan: {wrong[:5]}.")
        edges |= {(e["source"], e["output"], e["target"], e["input"]) for e in before["connections"] if e["target"] == base["MediaIn"]}
        actual = {(e["source"], e["output"], e["target"], e["input"]) for e in after["connections"]}
        if actual != edges:
            raise VerificationError(f"Fusion wiring differs from the plan: missing {sorted(edges - actual)[:5]}, "
                                    f"unexpected {sorted(actual - edges)[:5]}.")
        if any(nodes[name]["expressions"] or nodes[name]["pass_through"] for name in types):
            raise VerificationError("A new Fusion node has an expression or is bypassed.")

        def close(a, b):
            return type(a) in (int, float) and math.isclose(a, b, abs_tol=1e-6)

        def check_rect(name, x, y, width, height):
            p = nodes[name]["parameters"]
            px, py = _point(p.get("Center"), name + ".Center")
            if not (close(px, x) and close(py, y) and close(p.get("Width"), width) and close(p.get("Height"), height)):
                raise VerificationError(f"{name} position or size did not match the plan.")

        def check_color(name, rgb, alpha=1.0):
            p = nodes[name]["parameters"]
            if not all(close(p.get(k), v) for k, v in zip(("TopLeftRed", "TopLeftGreen", "TopLeftBlue", "TopLeftAlpha"), (*rgb, alpha))):
                raise VerificationError(f"{name} color did not match the plan.")

        P = layout["playhead_x"]
        for chunk in layout["chunks"]:
            for note in chunk["notes"]:
                check_rect(note["name"], note["x"], note["y"], note["width"], note["height"])
            check_color(chunk["chunk"], ROLL_COLOR)
            if not close(nodes[chunk["path"]]["parameters"].get("Y"), 0.5):
                raise VerificationError(f"{chunk['path']} Y did not match the plan.")
            keys = nodes[chunk["scroll"]].get("keyframes") or {}
            if set(keys) != {str(f) for f in chunk["keys"]} or any(not close(keys[str(f)].get("1"), v) for f, v in chunk["keys"].items()):
                raise VerificationError(f"{chunk['scroll']} keyframes did not match the plan.")
        check_rect(LIT_MASK, P / 2, 0.5, P, 2.0)
        check_rect(PLAYHEAD_MASK, P, 0.5, PLAYHEAD_WIDTH, 2.0)
        check_color(CANVAS, (0.0, 0.0, 0.0), 0.0)
        check_color(PLAYHEAD, PLAYHEAD_COLOR)
        if not close(nodes[LIT]["parameters"].get("Gain"), LIT_GAIN):
            raise VerificationError("Lit gain did not match the plan.")

        settings = _copy_settings(comp, _call(comp, "GetToolList", False))
        for name in original:
            expected = original_settings["Tools"][name]
            if name == base["MediaOut"]:
                expected = json.loads(json.dumps(expected))
                expected["Inputs"]["Input"] = {"__ctor": "Input", "SourceOp": PLAYHEAD_MERGE, "Source": "Output"}
            if settings["Tools"].get(name) != expected:
                raise VerificationError("An existing Fusion tool changed beyond the planned output connection.")

        # Evaluate the scroll where it matters: at both ends, mid-clip, and at
        # up to three note onsets per chunk, the left edge must be on the playhead.
        merges = {_call(t, "GetAttrs")["TOOLS_Name"]: t for t in _call(comp, "GetToolList", False).values()}
        fps = float(Fraction(layout["fps"]))
        rs, re = layout["render_start"], layout["render_end"]
        checks = []
        for chunk in layout["chunks"]:
            merge, k = merges[chunk["merge"]], chunk["index"]
            expected_x = lambda f: P + 0.5 + k - (f - rs) / (fps * layout["seconds_visible"])  # noqa: E731
            for frame in (rs, re, (rs + re) / 2):
                x, y = _point(_call(merge, "GetInput", "Center", frame), chunk["merge"] + ".Center")
                if not (close(x, expected_x(frame)) and close(y, 0.5)):
                    raise VerificationError(f"{chunk['merge']} scroll is off the plan at frame {frame} (eased, not linear?).")
            for note in [n for n in chunk["notes"] if n["onset"] is not None][:3]:
                frame = rs + (note["onset"] - layout["start_seconds"]) * fps
                x, _ = _point(_call(merge, "GetInput", "Center", frame), chunk["merge"] + ".Center")
                left_edge = x - 0.5 + note["x"] - note["width"] / 2
                if not close(left_edge, P):
                    raise VerificationError(f"{note['name']} does not reach the playhead at its note-on (frame {frame:.4f}).")
                checks.append({"note": note["name"], "seconds": note["onset"], "comp_frame": frame, "left_edge_x": left_edge})
        return checks

    def _add_markers(self, plan, state, layout, operation_id):
        if not layout["bar_markers"]:
            require_snapshot(self._editable(plan.clip_id)[0], plan.expected_snapshot_id)
            return []
        self._check_markers(state, layout)
        expected = [MarkerState(m["timeline_frame"] - state.timeline.start_frame, MARKER_COLOR, m["name"], m["note"], 1,
                                f"resolve-bridge:{operation_id}:bar:{m['bar']}") for m in layout["bar_markers"]]
        self._editable(plan.clip_id)
        _, timeline = self._adapter._context()
        for marker in expected:
            if _write(timeline, "AddMarker", marker.offset_frame, marker.color, marker.name, marker.note,
                      marker.duration_frames, marker.custom_data) is not True:
                raise VerificationError(f"Resolve declined the bar marker at offset {marker.offset_frame}.")
        after, _ = self._editable(plan.clip_id)
        if any(marker not in after.timeline.markers for marker in expected):
            raise VerificationError("A bar marker was not found exactly on reread.")
        remaining = tuple(m for m in after.timeline.markers if m not in expected)
        if replace(after, timeline=replace(after.timeline, markers=remaining)).snapshot_id != plan.expected_snapshot_id:
            raise VerificationError("Timeline state other than the bar markers changed.")
        return [asdict(m) for m in expected]
