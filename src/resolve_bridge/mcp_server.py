"""Model Context Protocol server (stdio) over the bridge's bounded operations.

Dependency-free: newline-delimited JSON-RPC 2.0 on stdin/stdout, as the MCP stdio
transport specifies. Only tools are exposed. Every tool maps onto an existing
read, plan, or journaled write; there is no script, expression, or generic
setter tool. Project-content reads and all writes require a project ID that is
in the configured allowlist and is the project currently open in Resolve.

Run: resolve-bridge-mcp [--config PATH]   (or python -m resolve_bridge.mcp_server)
"""

from collections import OrderedDict
from dataclasses import asdict
import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import traceback
import uuid

from . import __version__, calibration
from ._api import _call, _id, _text
from .config import ConfigError, ServerConfig  # noqa: F401  (re-exported)
from .diff import diff_states
from .errors import (InvalidOperationError, NoProjectError, UncalibratedBuildError, ProjectScopeError, ResolveBridgeError,
                     ResolveReadError, ResolveUnavailableError, ResolveWriteError,
                     ScriptingModuleUnavailableError)
from .fusion import FusionAdapter, FusionService, LowerThirdPlan
from .journal import OperationJournal
from .media import find_media
from . import midi_roll
from .midi_roll import MidiRollPlan, MidiRollService
from .operations import AppendClipPlan, EditingService
from .project import ProjectSetup
from .timecode import frames_to_seconds, frames_to_timecode, seconds_to_frames, timecode_to_frames

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "resolve-bridge"
MAX_CACHED = 64

INSTRUCTIONS = """DaVinci Resolve bridge with bounded, verified operations.
Start with resolve_status. Every project read or write needs a project_id from the
allowlist, and that project must be open in Resolve. Full timeline reads and all
timeline writes need the timeline to be active; use select_timeline explicitly.
Writes follow plan -> execute: plan_* tools are read-only and return a plan_id;
execute_* tools take that plan_id plus your own unique idempotency_key. Repeating
the same key and plan returns the stored result without editing again.
Frames are absolute timeline frames (a 01:00:00:00 start at 29.97 is frame 108000);
source ranges are half-open [in, out). Use convert_time instead of mental arithmetic.
If any write fails, do not retry it or invent a new key: call read_journal and
read_timeline, and report to the user. A failed write may be partly applied.
Writes work only on a calibrated Resolve build (resolve_status reports it); on any
other build the server is read-only and only the user can change that.
For a MIDI piano roll: read_midi_file first to pick a track, then the same
prepare -> read_fusion_graph -> plan -> execute path as the lower third."""


# ---------------------------------------------------------------- schemas

def _s(description, **extra):
    return {"type": "string", "minLength": 1, "maxLength": 256, "description": description, **extra}


PROJECT = _s("Stable Resolve project ID. Must be in the server allowlist and open in Resolve.", maxLength=128)
TIMELINE = _s("Stable timeline ID from list_timelines.", maxLength=128)
CLIP = _s("Stable timeline item (clip) ID from read_timeline.", maxLength=128)
SNAPSHOT = _s("snapshot_id from the most recent read_timeline; stale values are rejected.")
KEY = _s("Caller-chosen unique key for this write, e.g. '2026-10-05-append-intro'. "
         "Reusing it with the same request replays the stored result; never reuse it for a different request.")
PLAN_ID = _s("plan_id returned by the matching plan_* tool in this server session.", maxLength=64)
FRAME = {"type": "integer", "minimum": 0, "maximum": 2**31 - 1}
COLORS = ["Blue", "Green", "Red", "Yellow", "Cyan", "Purple", "Pink", "Orange", "White"]
PAGES = ["media", "photo", "cut", "edit", "fusion", "color", "fairlight", "deliver"]
MIDI_PATH = _s("Absolute path to a .mid/.midi file inside a folder listed in the server's midi_roots.", maxLength=1024)


def _obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS = [
    ("resolve_status", "Connection, Resolve build, the open project and whether it is allowlisted, the active timeline, and server policy. Call first.",
     _obj({}), READ),
    ("list_timelines", "Timeline names and stable IDs in the project, marking the active one. Does not change selection.",
     _obj({"project_id": PROJECT}, ["project_id"]), READ),
    ("read_timeline", "Full normalized edit state of the ACTIVE timeline: tracks, clips (absolute frames, source ranges), markers, and its snapshot_id.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE}, ["project_id", "timeline_id"]), READ),
    ("read_media", "Whole Media Pool inventory: bins, media IDs, file names, paths, rates, frame counts, online status. Can be large; prefer search_media.",
     _obj({"project_id": PROJECT}, ["project_id"]), READ),
    ("search_media", "Find Media Pool items by clip or file name (Unicode-normalized, case-insensitive). Returns every candidate; never chooses for you.",
     _obj({"project_id": PROJECT, "query": _s("Name or filename fragment."),
           "exact": {"type": "boolean", "description": "Exact name match instead of substring. Default false."}},
          ["project_id", "query"]), READ),
    ("convert_time", "Exact frame/timecode/elapsed-seconds conversion for a frame rate. Give exactly one of timecode, frame, seconds. No Resolve call.",
     _obj({"rate": _s("Frame-rate label as Resolve reports it, e.g. '24', '29.97', '29.97 DF'.", maxLength=16),
           "timecode": _s("HH:MM:SS:FF (NDF) or HH:MM:SS;FF (DF).", maxLength=16),
           "frame": FRAME,
           "seconds": _s("Elapsed seconds as decimal text, e.g. '12.5'.", maxLength=32),
           "rounding": {"type": "string", "enum": ["exact", "floor", "ceil", "nearest"],
                        "description": "Only for seconds; default exact refuses to round."}},
          ["rate"]), READ),
    ("compare_snapshot", "Re-read the active timeline and compare it with an earlier snapshot_id. If that earlier read happened in this server session, returns a clip/marker/track diff.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "since_snapshot_id": SNAPSHOT},
          ["project_id", "timeline_id", "since_snapshot_id"]), READ),
    ("read_journal", "Inspect the durable write journal: one operation in full by operation_id, or the most recent attempts. Use after any failed or uncertain write.",
     _obj({"operation_id": _s("Operation ID from a write result or a listing.", maxLength=64),
           "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "How many recent attempts to list. Default 20."}}), READ),
    ("plan_append_clip", "Read-only. Validate inserting media source frames [source_in, source_out) at an absolute record frame on an empty range of an existing track. Returns a plan_id.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "media_id": _s("Stable media ID from search_media.", maxLength=128),
           "source_in_frame": FRAME, "source_out_frame": {**FRAME, "description": "Exclusive end."},
           "track_type": {"type": "string", "enum": ["video", "audio"]},
           "track_index": {"type": "integer", "minimum": 1, "maximum": 64},
           "record_frame": {**FRAME, "description": "Absolute timeline frame where the clip starts."},
           "expected_snapshot_id": SNAPSHOT},
          ["project_id", "timeline_id", "media_id", "source_in_frame", "source_out_frame", "track_type",
           "track_index", "record_frame", "expected_snapshot_id"]), READ),
    ("execute_append_clip", "Write. Execute a plan from plan_append_clip, then verify exact media, range, destination and that nothing else changed.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "plan_id": PLAN_ID, "idempotency_key": KEY},
          ["project_id", "timeline_id", "plan_id", "idempotency_key"]), WRITE),
    ("add_marker", "Write. Add one timeline marker at an absolute frame inside the timeline; refuses to overwrite an existing marker.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "frame": FRAME,
           "color": {"type": "string", "enum": COLORS}, "name": _s("Marker name."),
           "note": {"type": "string", "maxLength": 4096}, "expected_snapshot_id": SNAPSHOT, "idempotency_key": KEY},
          ["project_id", "timeline_id", "frame", "color", "name", "expected_snapshot_id", "idempotency_key"]), WRITE),
    ("ensure_video_track", "Write. Add video tracks until the timeline has at least `index` (max 16). Verifies existing tracks are untouched.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "index": {"type": "integer", "minimum": 1, "maximum": 16},
           "idempotency_key": KEY}, ["project_id", "timeline_id", "index", "idempotency_key"]), WRITE),
    ("select_timeline", "Write (UI state). Make a timeline active so it can be read in full or edited. Journaled; returns the previously active timeline ID for restoring.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "idempotency_key": KEY},
          ["project_id", "timeline_id", "idempotency_key"]), WRITE),
    ("create_test_timeline", "Write. Create an empty timeline named AI_BRIDGE_TEST_* in AI Bridge / 90 Tests, then restore the previous selection.",
     _obj({"project_id": PROJECT, "name": _s("Must start with AI_BRIDGE_TEST_.", maxLength=120, pattern="^AI_BRIDGE_TEST_"),
           "idempotency_key": KEY}, ["project_id", "name", "idempotency_key"]), WRITE),
    ("save_and_backup", "Write. Save the project and export a .drp backup beside the journal; checks archive integrity. Restore is untested.",
     _obj({"project_id": PROJECT, "label": _s("1-64 letters, digits, _ or -; must be new.", maxLength=64, pattern="^[A-Za-z0-9_-]{1,64}$"),
           "idempotency_key": KEY}, ["project_id", "label", "idempotency_key"]), WRITE),
    ("list_fusion_compositions", "Fusion composition names on one video clip. Works on inactive timelines; no selection change.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "clip_id": CLIP}, ["project_id", "timeline_id", "clip_id"]), READ),
    ("read_fusion_graph", "Normalized Fusion node graph (nodes, connections, key parameters, keyframes) and its graph_id fingerprint.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "clip_id": CLIP, "comp_name": _s("Composition name.")},
          ["project_id", "timeline_id", "clip_id", "comp_name"]), READ),
    ("create_fusion_composition", "Write. Add a first Fusion composition to a clip that has none (AI_BRIDGE_TEST_ timelines only).",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "clip_id": CLIP, "expected_snapshot_id": SNAPSHOT,
           "idempotency_key": KEY}, ["project_id", "timeline_id", "clip_id", "expected_snapshot_id", "idempotency_key"]), WRITE),
    ("prepare_fusion_composition", "Write (UI state). Move the playhead to the clip and load its composition on the Fusion page, which Resolve needs before graph edits render. Returns the previous page and timecode for restore_fusion_view.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "clip_id": CLIP, "comp_name": _s("Composition name."),
           "idempotency_key": KEY}, ["project_id", "timeline_id", "clip_id", "comp_name", "idempotency_key"]), WRITE),
    ("plan_lower_third", "Read-only. Validate the animated Text+ lower-third recipe on a simple MediaIn->MediaOut composition. Returns a plan_id.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "clip_id": CLIP, "comp_name": _s("Composition name."),
           "expected_snapshot_id": SNAPSHOT, "expected_graph_id": _s("graph_id from read_fusion_graph."),
           "text": {"type": "string", "minLength": 1, "maxLength": 160, "description": "1-160 characters, at most two lines."},
           "x": {"type": "number", "minimum": 0.1, "maximum": 0.9, "description": "Normalized horizontal center. Default 0.5."},
           "y": {"type": "number", "minimum": 0.1, "maximum": 0.35, "description": "Normalized vertical center. Default 0.18."},
           "size": {"type": "number", "minimum": 0.02, "maximum": 0.12, "description": "Text size. Default 0.065."},
           "fade_frames": {"type": "integer", "minimum": 1, "maximum": 120, "description": "Fade in/out length. Default 12."}},
          ["project_id", "timeline_id", "clip_id", "comp_name", "expected_snapshot_id", "expected_graph_id", "text"]), READ),
    ("execute_lower_third", "Write. Build the planned lower third (Text+, Merge, opacity spline) inside one Undo group and verify nodes, wiring, values and keyframes. Requires prepare_fusion_composition first.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "plan_id": PLAN_ID, "idempotency_key": KEY},
          ["project_id", "timeline_id", "plan_id", "idempotency_key"]), WRITE),
    ("read_midi_file", "Read-only, no Resolve call. Parse a MIDI file: tracks (1-based numbers, names, note counts, channels, pitch range), tempo map, time signatures, length. Use it to choose track/channel for the piano roll.",
     _obj({"midi_path": MIDI_PATH}, ["midi_path"]), READ),
    ("plan_midi_piano_roll", "Read-only. Validate a scrolling MIDI piano roll on a simple MediaIn->MediaOut composition: one rectangle per note, scrolling so each note reaches a playhead line at its tempo-mapped note-on and lighting from then on, optional per-bar timeline markers. Fails with counts if the selection exceeds the node cap (256). Returns a plan_id and a layout summary.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "clip_id": CLIP, "comp_name": _s("Composition name."),
           "expected_snapshot_id": SNAPSHOT, "expected_graph_id": _s("graph_id from read_fusion_graph."), "midi_path": MIDI_PATH,
           "track": {"type": "integer", "minimum": 1, "maximum": 1024, "description": "1-based track number from read_midi_file. Default all tracks."},
           "channel": {"type": "integer", "minimum": 1, "maximum": 16, "description": "1-based MIDI channel. Default all."},
           "skip_drums": {"type": "boolean", "description": "Leave out channel 10 (GM drums). Default true."},
           "start_seconds": {"type": "number", "minimum": 0, "maximum": 3600, "description": "Song time at the clip's first frame. Default 0. Notes starting earlier are not drawn."},
           "end_seconds": {"type": "number", "minimum": 0, "maximum": 3600, "description": "Last note-on to draw, in song seconds. Default: whatever can reach the screen during the clip."},
           "min_pitch": {"type": "integer", "minimum": 0, "maximum": 127, "description": "Lowest MIDI note drawn (60 = C4). With max_pitch sets the rows exactly; default fits the notes, padded to an octave."},
           "max_pitch": {"type": "integer", "minimum": 0, "maximum": 127, "description": "Highest MIDI note drawn."},
           "seconds_visible": {"type": "number", "minimum": 2, "maximum": 20, "description": "Seconds of music across the frame width (scroll speed). Default 6."},
           "playhead_x": {"type": "number", "minimum": 0.1, "maximum": 0.9, "description": "Playhead line position, fraction of frame width from the left. Default 0.25."},
           "bar_markers": {"type": "boolean", "description": "Also add a Purple timeline marker on every bar inside the clip. Default false."}},
          ["project_id", "timeline_id", "clip_id", "comp_name", "expected_snapshot_id", "expected_graph_id", "midi_path"]), READ),
    ("execute_midi_piano_roll", "Write. Build the planned piano roll inside one Undo group (and its bar markers, if planned), then verify every node, connection, value and keyframe, and that each sampled note's left edge is on the playhead at its note-on frame. Requires prepare_fusion_composition first.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "plan_id": PLAN_ID, "idempotency_key": KEY},
          ["project_id", "timeline_id", "plan_id", "idempotency_key"]), WRITE),
    ("restore_fusion_view", "Write (UI state). Return to a saved page and playhead timecode, e.g. the values prepare_fusion_composition returned.",
     _obj({"project_id": PROJECT, "timeline_id": TIMELINE, "page": {"type": "string", "enum": PAGES},
           "timecode": _s("Timeline timecode to restore.", maxLength=16), "idempotency_key": KEY},
          ["project_id", "timeline_id", "page", "timecode", "idempotency_key"]), WRITE),
]


class ArgumentError(ResolveBridgeError):
    code = "invalid_arguments"


def validate(schema: dict, value, path: str = "arguments") -> None:
    """The subset of JSON Schema the tool definitions above use, strictly."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ArgumentError(f"{path} must be an object.")
        unknown = set(value) - set(schema.get("properties", {}))
        if unknown and schema.get("additionalProperties") is False:
            raise ArgumentError(f"{path} has unknown field(s): {', '.join(sorted(unknown))}.")
        for name in schema.get("required", ()):
            if name not in value:
                raise ArgumentError(f"{path}.{name} is required.")
        for name, item in value.items():
            if name in schema.get("properties", {}):
                validate(schema["properties"][name], item, f"{path}.{name}")
        return
    if kind == "string":
        if not isinstance(value, str):
            raise ArgumentError(f"{path} must be a string.")
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 10**6):
            raise ArgumentError(f"{path} length must be {schema.get('minLength', 0)}-{schema.get('maxLength')}.")
        if "pattern" in schema:
            if re.search(schema["pattern"], value) is None:
                raise ArgumentError(f"{path} must match {schema['pattern']}.")
    elif kind in ("integer", "number"):
        ok = type(value) is int if kind == "integer" else (type(value) in (int, float) and math.isfinite(value))
        if not ok:
            raise ArgumentError(f"{path} must be {'an integer' if kind == 'integer' else 'a finite number'}.")
        if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise ArgumentError(f"{path} must be between {schema.get('minimum')} and {schema.get('maximum')}.")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ArgumentError(f"{path} must be a boolean.")
    if "enum" in schema and value not in schema["enum"]:
        raise ArgumentError(f"{path} must be one of {schema['enum']}.")


class _Bounded(OrderedDict):
    def put(self, key, value):
        self[key] = value
        self.move_to_end(key)
        while len(self) > MAX_CACHED:
            self.popitem(last=False)


class BridgeTools:
    """Tool handlers. Holds the lazy Resolve connection, issued plans, and recent reads."""

    def __init__(self, config: ServerConfig, connect=None, journal: OperationJournal | None = None):
        if connect is None:
            from .connection import connect
        self.config = config
        self._connect = connect
        self._adapter_cache = None
        self._journal = journal
        self._plans = _Bounded()
        self._states = _Bounded()

    # -- plumbing
    @property
    def journal(self) -> OperationJournal:
        if self._journal is None:
            self._journal = OperationJournal(self.config.journal_path)
        return self._journal

    def adapter(self):
        if self._adapter_cache is None:
            self._adapter_cache = self._connect()
        return self._adapter_cache

    def drop_connection(self):
        self._adapter_cache = None

    def _allowed(self, project_id: str, *, write: bool = False) -> dict:
        entry = self.config.allowed_projects.get(project_id)
        if entry is None:
            raise ProjectScopeError("Project ID is not in the MCP server allowlist. Ask the user to add it to the config.")
        if write and not entry["allow_writes"]:
            raise ProjectScopeError("This project is allowlisted read-only. Writes need allow_writes: true in the config.")
        return entry

    def _scoped(self, project_id: str, *, write: bool = False):
        self._allowed(project_id, write=write)
        adapter = self.adapter()
        if _id(adapter._project()) != project_id:
            raise ProjectScopeError("The project open in Resolve is not the requested project. Ask the user to open it.")
        if write:
            version = _text(_call(adapter._resolve, "GetVersionString"), "GetVersionString")
            if not calibration.is_calibrated(version):
                raise UncalibratedBuildError(
                    f"Resolve {version} is not a calibrated build ({calibration.describe()}), so this server is "
                    "read-only. Ask the user to follow docs/RECALIBRATION.md; do not try another write tool.")
        return adapter

    def _write_timeline(self, adapter, timeline_id: str) -> None:
        prefix = self.config.write_timeline_prefix
        names = {entry["id"]: entry["name"] for entry in adapter.list_timelines()}
        if timeline_id not in names:
            raise InvalidOperationError("The requested timeline ID is not in the open project.")
        if prefix and not names[timeline_id].startswith(prefix):
            raise InvalidOperationError(f"Server policy allows timeline writes only on timelines named {prefix}*.")

    def _editing(self, project_id, timeline_id, *, write=False):
        adapter = self._scoped(project_id, write=write)
        if write:
            self._write_timeline(adapter, timeline_id)
        return EditingService(adapter, project_id, timeline_id, self.journal)

    def _remember(self, state):
        self._states.put(state.snapshot_id, state)
        return state

    def _plan(self, plan_id, kind, project_id, timeline_id):
        plan = self._plans.get(plan_id)
        if not isinstance(plan, kind):
            raise InvalidOperationError("Unknown plan_id for this tool. Plans live only for this server session; plan again.")
        if (plan.project_id, plan.timeline_id) != (project_id, timeline_id):
            raise ProjectScopeError("The plan belongs to a different project or timeline.")
        return plan

    # -- reads
    def resolve_status(self):
        adapter = self.adapter()
        version = _text(_call(adapter._resolve, "GetVersionString"), "GetVersionString")
        result = {"resolve_version": version, "product": _text(_call(adapter._resolve, "GetProductName"), "GetProductName"),
                  "calibrated_build": calibration.is_calibrated(version), "calibrated_builds": list(calibration.calibrated_builds()),
                  "writes_available": calibration.is_calibrated(version), "project": None, "active_timeline": None,
                  "allowlist": [{"id": key, **value} for key, value in self.config.allowed_projects.items()],
                  "write_timeline_prefix": self.config.write_timeline_prefix, "journal_path": str(self.config.journal_path),
                  "backup_dir": str(self.config.effective_backup_dir()),
                  "config_file": str(self.config.source) if self.config.source else None}
        try:
            project = adapter._project()
        except NoProjectError:
            return result
        project_id = _id(project)
        entry = self.config.allowed_projects.get(project_id)
        result["project"] = {"id": project_id, "name": _text(_call(project, "GetName"), "Project.GetName"),
                             "allowlisted": entry is not None, "writes_allowed": bool(entry and entry["allow_writes"])}
        timeline = _call(project, "GetCurrentTimeline")
        if entry is not None and timeline is not None:
            result["active_timeline"] = {"id": _id(timeline), "name": _text(_call(timeline, "GetName"), "Timeline.GetName")}
        return result

    def list_timelines(self, project_id):
        adapter = self._scoped(project_id)
        timelines = adapter.list_timelines()
        current = _call(adapter._project(), "GetCurrentTimeline")
        active = _id(current) if current is not None else None
        return {"project_id": project_id, "timelines": [{**entry, "active": entry["id"] == active} for entry in timelines]}

    def read_timeline(self, project_id, timeline_id):
        return self._remember(self._editing(project_id, timeline_id).read()).to_dict()

    def read_media(self, project_id):
        inventory = self._scoped(project_id).read_media()
        if inventory.project.id != project_id:
            raise ProjectScopeError("Project changed during the media read.")
        return inventory.to_dict()

    def search_media(self, project_id, query, exact=False):
        inventory = self._scoped(project_id).read_media()
        if inventory.project.id != project_id:
            raise ProjectScopeError("Project changed during the media read.")
        matches = find_media(inventory, query, exact=exact)
        return {"query": query, "exact": exact, "count": len(matches), "matches": [asdict(item) for item in matches]}

    def convert_time(self, rate, timecode=None, frame=None, seconds=None, rounding=None):
        given = [name for name, value in (("timecode", timecode), ("frame", frame), ("seconds", seconds)) if value is not None]
        if len(given) != 1:
            raise ArgumentError("Give exactly one of timecode, frame, or seconds.")
        if rounding is not None and seconds is None:
            raise ArgumentError("rounding applies only to seconds.")
        if timecode is not None:
            frame = timecode_to_frames(timecode, rate)
        elif seconds is not None:
            frame = seconds_to_frames(seconds, rate, rounding=rounding or "exact")
        elapsed = frames_to_seconds(frame, rate)
        return {"rate": rate, "frame": frame, "timecode": frames_to_timecode(frame, rate),
                "seconds_exact": f"{elapsed.numerator}/{elapsed.denominator}", "seconds": float(elapsed)}

    def compare_snapshot(self, project_id, timeline_id, since_snapshot_id):
        current = self._remember(self._editing(project_id, timeline_id).read())
        result = {"since_snapshot_id": since_snapshot_id, "current_snapshot_id": current.snapshot_id,
                  "matches": current.snapshot_id == since_snapshot_id}
        baseline = self._states.get(since_snapshot_id)
        if baseline is None:
            result["diff"] = None
            if not result["matches"]:
                result["note"] = "That snapshot was not read in this server session, so only the mismatch is known."
        else:
            result["diff"] = diff_states(baseline, current)
        return result

    def read_journal(self, operation_id=None, limit=None):
        if operation_id is not None:
            if limit is not None:
                raise ArgumentError("Give operation_id or limit, not both.")
            record = self.journal.get(operation_id)
            if record is None:
                raise InvalidOperationError("No journal record has that operation ID.")
            return record
        return {"journal_path": str(self.journal.path), "operations": self.journal.recent(limit or 20)}

    # -- editing writes
    def plan_append_clip(self, project_id, timeline_id, expected_snapshot_id, **arguments):
        plan = self._editing(project_id, timeline_id).plan_append_clip(expected_snapshot_id=expected_snapshot_id, **arguments)
        self._plans.put(plan.plan_id, plan)
        return {"plan_id": plan.plan_id, "plan": plan.to_dict(),
                "record_end_frame": plan.record_frame + plan.source_out_frame - plan.source_in_frame}

    def execute_append_clip(self, project_id, timeline_id, plan_id, idempotency_key):
        self._allowed(project_id, write=True)
        plan = self._plan(plan_id, AppendClipPlan, project_id, timeline_id)
        return self._editing(project_id, timeline_id, write=True).execute_append_clip(plan, idempotency_key=idempotency_key)

    def add_marker(self, project_id, timeline_id, frame, color, name, expected_snapshot_id, idempotency_key, note=""):
        return self._editing(project_id, timeline_id, write=True).add_marker(
            frame=frame, color=color, name=name, note=note, expected_snapshot_id=expected_snapshot_id,
            idempotency_key=idempotency_key)

    def ensure_video_track(self, project_id, timeline_id, index, idempotency_key):
        return self._editing(project_id, timeline_id, write=True).ensure_video_track(index, idempotency_key=idempotency_key)

    def select_timeline(self, project_id, timeline_id, idempotency_key):
        adapter = self._scoped(project_id, write=True)
        return ProjectSetup(adapter, project_id, self.journal).select_timeline(timeline_id, idempotency_key=idempotency_key)

    def create_test_timeline(self, project_id, name, idempotency_key):
        adapter = self._scoped(project_id, write=True)
        return ProjectSetup(adapter, project_id, self.journal).create_test_timeline(name, idempotency_key=idempotency_key)

    def save_and_backup(self, project_id, label, idempotency_key):
        adapter = self._scoped(project_id, write=True)
        return ProjectSetup(adapter, project_id, self.journal, backup_dir=self.config.effective_backup_dir()).save_and_backup(
            label, idempotency_key=idempotency_key)

    # -- Fusion
    def _fusion(self, project_id, timeline_id, *, write=False):
        adapter = self._scoped(project_id, write=write)
        if write:
            self._write_timeline(adapter, timeline_id)
            return FusionService(adapter, project_id, timeline_id, self.journal)
        return FusionAdapter(adapter, project_id, timeline_id)

    def list_fusion_compositions(self, project_id, timeline_id, clip_id):
        return {"clip_id": clip_id, "compositions": list(self._fusion(project_id, timeline_id).list_compositions(clip_id))}

    def read_fusion_graph(self, project_id, timeline_id, clip_id, comp_name):
        return self._fusion(project_id, timeline_id).read_graph(clip_id, comp_name)

    def create_fusion_composition(self, project_id, timeline_id, clip_id, expected_snapshot_id, idempotency_key):
        return self._fusion(project_id, timeline_id, write=True).create_composition(
            clip_id, expected_snapshot_id=expected_snapshot_id, idempotency_key=idempotency_key)

    def prepare_fusion_composition(self, project_id, timeline_id, clip_id, comp_name, idempotency_key):
        return self._fusion(project_id, timeline_id, write=True).prepare_composition(clip_id, comp_name, idempotency_key=idempotency_key)

    def plan_lower_third(self, project_id, timeline_id, **arguments):
        # Planning reads only, but FusionService carries the write-scope checks
        # (calibrated build, test timeline, unlocked track) a plan must satisfy.
        self._allowed(project_id, write=True)
        plan = self._fusion(project_id, timeline_id, write=True).plan_lower_third(**arguments)
        plan_id = str(uuid.uuid4())
        self._plans.put(plan_id, plan)
        return {"plan_id": plan_id, "plan": plan.to_dict()}

    def execute_lower_third(self, project_id, timeline_id, plan_id, idempotency_key):
        self._allowed(project_id, write=True)
        plan = self._plan(plan_id, LowerThirdPlan, project_id, timeline_id)
        return self._fusion(project_id, timeline_id, write=True).execute_lower_third(plan, idempotency_key=idempotency_key)

    # -- MIDI piano roll
    def _midi_path(self, midi_path):
        if not self.config.midi_roots:
            raise ProjectScopeError("No midi_roots are configured, so no MIDI file can be read. Ask the user to add the folder to the config.")
        resolved = Path(midi_path).expanduser().resolve()
        if not any(resolved.is_relative_to(root) for root in self.config.midi_roots):
            raise ProjectScopeError("That MIDI file is outside the configured midi_roots.")
        return str(resolved)

    def read_midi_file(self, midi_path):
        path = self._midi_path(midi_path)
        data, digest = midi_roll.read_midi_file(path)
        return {"midi_path": path, "sha256": digest, **midi_roll.midi.summary(midi_roll.parse_midi(data))}

    def plan_midi_piano_roll(self, project_id, timeline_id, midi_path, **arguments):
        # Like plan_lower_third: read-only, but the plan checks write scope.
        self._allowed(project_id, write=True)
        path = self._midi_path(midi_path)
        adapter = self._scoped(project_id, write=True)
        self._write_timeline(adapter, timeline_id)
        plan, layout = MidiRollService(adapter, project_id, timeline_id, self.journal).plan_midi_roll(midi_path=path, **arguments)
        plan_id = str(uuid.uuid4())
        self._plans.put(plan_id, plan)
        summary = {key: value for key, value in layout.items() if key != "chunks"}
        summary["chunks"] = [{"index": c["index"], "notes": len(c["notes"])} for c in layout["chunks"]]
        return {"plan_id": plan_id, "plan": plan.to_dict(), "layout": summary,
                "caps": {"nodes": midi_roll.MAX_NODES, "note_rects": midi_roll.MAX_NOTE_RECTS, "chunks": midi_roll.MAX_CHUNKS}}

    def execute_midi_piano_roll(self, project_id, timeline_id, plan_id, idempotency_key):
        self._allowed(project_id, write=True)
        plan = self._plan(plan_id, MidiRollPlan, project_id, timeline_id)
        self._midi_path(plan.midi_path)
        adapter = self._scoped(project_id, write=True)
        self._write_timeline(adapter, timeline_id)
        return MidiRollService(adapter, project_id, timeline_id, self.journal).execute_midi_roll(plan, idempotency_key=idempotency_key)

    def restore_fusion_view(self, project_id, timeline_id, page, timecode, idempotency_key):
        context = {"project_id": project_id, "timeline_id": timeline_id, "page": page, "timecode": timecode}
        return self._fusion(project_id, timeline_id, write=True).restore_view(context, idempotency_key=idempotency_key)


_RECONNECT = (ResolveUnavailableError, ScriptingModuleUnavailableError, ResolveReadError, ResolveWriteError)


class MCPServer:
    """JSON-RPC message handling. One request at a time, in arrival order."""

    def __init__(self, tools: BridgeTools, log=None):
        self.tools = tools
        self._log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        self._specs = {name: (description, schema, annotations) for name, description, schema, annotations in TOOLS}
        self.protocol_version = None

    def tool_list(self):
        return [{"name": name, "title": name.replace("_", " ").capitalize(), "description": description,
                 "inputSchema": schema, "annotations": annotations}
                for name, (description, schema, annotations) in self._specs.items()]

    def call_tool(self, name: str, arguments) -> dict:
        _, schema, _ = self._specs[name]
        try:
            arguments = {} if arguments is None else arguments
            validate(schema, arguments)
            result = getattr(self.tools, name)(**arguments)
            payload, is_error = result, False
        except ResolveBridgeError as exc:
            if isinstance(exc, _RECONNECT):
                self.tools.drop_connection()
            payload, is_error = {"error": exc.to_dict()}, True
        except Exception as exc:  # A tool failure must never take the server down.
            self._log(traceback.format_exc())
            self.tools.drop_connection()
            payload, is_error = {"error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}}, True
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)
        response = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if isinstance(payload, dict):
            response["structuredContent"] = json.loads(text)
        return response

    def handle(self, message):
        """Return a response dict, or None for notifications and responses."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        method, request_id = message.get("method"), message.get("id")
        if "id" not in message:  # notification
            return None
        if not isinstance(method, str):
            return _error(request_id, -32600, "Invalid Request")
        params = message.get("params")
        params = {} if params is None else params
        if not isinstance(params, dict):
            return _error(request_id, -32602, "params must be an object")
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.protocol_version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(request_id, {"protocolVersion": self.protocol_version,
                                        "capabilities": {"tools": {"listChanged": False}},
                                        "serverInfo": {"name": SERVER_NAME, "version": __version__},
                                        "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": self.tool_list()})
        if method == "tools/call":
            name = params.get("name")
            if name not in self._specs:
                return _error(request_id, -32602, f"Unknown tool: {name}")
            return _result(request_id, self.call_tool(name, params.get("arguments")))
        return _error(request_id, -32601, f"Method not found: {method}")

    def serve(self, reader, writer):
        """reader yields bytes or str lines; writer receives one JSON line per response."""
        for raw in reader:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = _error(None, -32700, "Parse error")
            else:
                if isinstance(message, list):
                    response = _error(None, -32600, "Batches are not supported")
                else:
                    response = self.handle(message)
            if response is not None:
                writer.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")
                writer.flush()


def _result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="resolve-bridge-mcp", description="Resolve bridge MCP server (stdio).")
    parser.add_argument("--config", type=Path, default=None,
                        help="Config JSON. Default: $RESOLVE_BRIDGE_CONFIG, else the user config folder; "
                             "with no file there the server starts with an empty allowlist.")
    parser.add_argument("--version", action="version", version=f"resolve-bridge {__version__}")
    args = parser.parse_args(argv)
    try:
        config = ServerConfig.resolve(args.config)
    except ConfigError as exc:
        print(f"resolve-bridge MCP: {exc}", file=sys.stderr)
        return 2
    calibration.register(config.calibrated_builds)
    # The protocol owns stdout. Resolve's native wrapper prints diagnostics to
    # fd 1 (e.g. the CopySettings NameError), which would corrupt the stream, so
    # keep a private duplicate for protocol messages and point fd 1 at stderr.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    MCPServer(BridgeTools(config)).serve(sys.stdin.buffer, protocol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
