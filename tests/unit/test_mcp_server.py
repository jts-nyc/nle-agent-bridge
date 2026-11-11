import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resolve_bridge.errors import ResolveUnavailableError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.mcp_server import (PROTOCOL_VERSIONS, TOOLS, BridgeTools, ConfigError, MCPServer,
                                       ServerConfig, validate, ArgumentError)
from test_fusion import fusion  # noqa: F401  (pytest fixture reused for the Fusion tools)
from test_midi_roll import DEMO, roll  # noqa: F401  (pytest fixture reused for the MIDI tools)

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "project-1"
TIMELINE = "timeline-1"


def api(**values):
    return SimpleNamespace(**{name: Mock(return_value=value) for name, value in values.items()})


def make_config(tmp_path, *, allow_writes=True, prefix="AI_BRIDGE_TEST_", extra=()):
    projects = [{"id": PROJECT, "name": "AI Bridge Test", "allow_writes": allow_writes}, *extra]
    return ServerConfig.from_dict({"allowed_projects": projects, "journal_path": "journal.sqlite3",
                                   "write_timeline_prefix": prefix}, base=tmp_path)


@pytest.fixture
def bridge(scene, tmp_path):
    """The operations-test scene, served through MCP with a recording connect()."""
    scene.timeline.GetName.return_value = "AI_BRIDGE_TEST_MCP"
    scene.media.GetName = Mock(return_value="Interview_A.mov")
    scene.media.GetClipProperty = Mock(return_value={"File Name": "Interview_A.mov", "File Path": "/fake/Interview_A.mov",
                                                     "Frames": "1000", "FPS": "24", "Type": "Video + Audio",
                                                     "Start": "0", "Online Status": "Online"})
    folder = api(GetUniqueId="root", GetName="Master", GetClipList=[scene.media], GetSubFolderList=[])
    pool = api(GetRootFolder=folder)
    scene.project.GetMediaPool = Mock(return_value=pool)
    tracks = {"video": [scene.item], "audio": []}
    scene.timeline.GetItemListInTrack.side_effect = lambda kind, index: tracks[kind]

    def append(clips):
        clip = clips[0]
        duration = clip["endFrame"] - clip["startFrame"]
        item = api(GetUniqueId="new-item", GetName="Interview_A.mov", GetMediaPoolItem=scene.media,
                   GetStart=clip["recordFrame"], GetEnd=clip["recordFrame"] + duration, GetDuration=duration,
                   GetSourceStartFrame=clip["startFrame"], GetSourceEndFrame=min(clip["endFrame"], 999))
        tracks["video" if clip["mediaType"] == 1 else "audio"].append(item)
        scene.timeline.GetEndFrame.return_value = max(scene.timeline.GetEndFrame.return_value, clip["recordFrame"] + duration)
        return [item]

    def marker(frame, color, name, note, duration, custom_data):
        scene.timeline.GetMarkers.return_value[frame] = {"color": color, "name": name, "note": note,
                                                         "duration": duration, "customData": custom_data}
        return True

    pool.AppendToTimeline = Mock(side_effect=append)
    scene.timeline.AddMarker = Mock(side_effect=marker)
    connect = Mock(return_value=scene.adapter)
    tools = BridgeTools(make_config(tmp_path), connect=connect, journal=OperationJournal(tmp_path / "j.db"))
    return SimpleNamespace(scene=scene, pool=pool, connect=connect, tools=tools, server=MCPServer(tools, log=Mock()))


def rpc(server, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def call(server, tool, **arguments):
    response = rpc(server, "tools/call", {"name": tool, "arguments": arguments})
    result = response["result"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["isError"], result["structuredContent"]


def ok(server, tool, **arguments):
    is_error, payload = call(server, tool, **arguments)
    assert not is_error, payload
    return payload


def error_code(server, tool, **arguments):
    is_error, payload = call(server, tool, **arguments)
    assert is_error, payload
    return payload["error"]["code"]


def snapshot(bridge):
    return ok(bridge.server, "read_timeline", project_id=PROJECT, timeline_id=TIMELINE)["snapshot_id"]


def append_plan(bridge, **changes):
    arguments = {"project_id": PROJECT, "timeline_id": TIMELINE, "media_id": "media-1", "source_in_frame": 72,
                 "source_out_frame": 168, "track_type": "video", "track_index": 1, "record_frame": 86640,
                 "expected_snapshot_id": snapshot(bridge)} | changes
    return ok(bridge.server, "plan_append_clip", **arguments)


# ------------------------------------------------------------ protocol

def test_initialize_negotiates_version_and_advertises_tools_only(bridge):
    response = rpc(bridge.server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "resolve-bridge" and "plan" in result["instructions"]
    unknown = rpc(bridge.server, "initialize", {"protocolVersion": "1999-01-01"})
    assert unknown["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]
    bridge.connect.assert_not_called()


def test_notifications_ping_and_unknown_methods(bridge):
    assert bridge.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(bridge.server, "ping")["result"] == {}
    assert rpc(bridge.server, "resources/list")["error"]["code"] == -32601
    assert rpc(bridge.server, "tools/call", {"name": "run_script", "arguments": {}})["error"]["code"] == -32602
    assert bridge.server.handle({"id": 3, "method": "ping"})["error"]["code"] == -32600
    assert rpc(bridge.server, "tools/list", params=[])["error"]["code"] == -32602


def test_serve_loop_handles_parse_errors_batches_and_blank_lines(bridge):
    lines = ["\n", "not json\n", "[]\n", json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n"]
    written = []
    writer = SimpleNamespace(write=written.append, flush=lambda: None)
    bridge.server.serve(iter(lines), writer)
    responses = [json.loads(line) for line in written]
    assert [r.get("error", {}).get("code") for r in responses] == [-32700, -32600, None]
    assert responses[2] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in written)


def test_tool_list_is_bounded_and_typed(bridge):
    tools = {tool["name"]: tool for tool in rpc(bridge.server, "tools/list")["result"]["tools"]}
    assert len(tools) == len(TOOLS) == 25
    for name, tool in tools.items():
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert not any(word in name for word in ("script", "eval", "exec_", "python", "lua", "expression", "delete"))
        if not tool["annotations"]["readOnlyHint"]:
            assert {"project_id", "idempotency_key"} <= set(schema["required"]), name
            assert tool["annotations"]["destructiveHint"] is False
    read_only = {name for name, tool in tools.items() if tool["annotations"]["readOnlyHint"]}
    assert {"plan_append_clip", "plan_lower_third", "read_timeline", "compare_snapshot",
            "read_midi_file", "plan_midi_piano_roll"} <= read_only
    assert {"execute_append_clip", "add_marker", "save_and_backup", "execute_lower_third",
            "execute_midi_piano_roll"}.isdisjoint(read_only)


# ------------------------------------------------------------ scope

def test_status_reports_allowlist_without_requiring_project_id(bridge):
    status = ok(bridge.server, "resolve_status")
    assert status["calibrated_build"] is True
    assert status["project"] == {"id": PROJECT, "name": "AI Bridge Test", "allowlisted": True, "writes_allowed": True}
    assert status["active_timeline"] == {"id": TIMELINE, "name": "AI_BRIDGE_TEST_MCP"}


def test_status_hides_timeline_of_unlisted_project(bridge):
    bridge.scene.project.GetUniqueId.return_value = "client-project"
    status = ok(bridge.server, "resolve_status")
    assert status["project"]["allowlisted"] is False and status["active_timeline"] is None


def test_unlisted_project_is_rejected_before_connecting(bridge):
    for name, extra in (("read_timeline", {"timeline_id": TIMELINE}), ("list_timelines", {}),
                        ("search_media", {"query": "x"}), ("read_media", {})):
        assert error_code(bridge.server, name, project_id="client-project", **extra) == "project_scope_mismatch"
    bridge.connect.assert_not_called()


def test_allowlisted_id_must_be_the_open_project(bridge, tmp_path):
    other = {"id": "other-project", "name": "Other", "allow_writes": True}
    bridge.tools.config = make_config(tmp_path, extra=[other])
    assert error_code(bridge.server, "list_timelines", project_id="other-project") == "project_scope_mismatch"
    timelines = ok(bridge.server, "list_timelines", project_id=PROJECT)["timelines"]
    assert timelines == [{"id": TIMELINE, "name": "AI_BRIDGE_TEST_MCP", "active": True}]


def test_read_only_project_can_plan_but_not_write(bridge, tmp_path):
    bridge.tools.config = make_config(tmp_path, allow_writes=False)
    proposed = append_plan(bridge)
    assert error_code(bridge.server, "execute_append_clip", project_id=PROJECT, timeline_id=TIMELINE,
                      plan_id=proposed["plan_id"], idempotency_key="k") == "project_scope_mismatch"
    for name, extra in (("add_marker", {"timeline_id": TIMELINE, "frame": 86500, "color": "Blue", "name": "n", "expected_snapshot_id": "s"}),
                        ("save_and_backup", {"label": "b1"}), ("select_timeline", {"timeline_id": TIMELINE})):
        assert error_code(bridge.server, name, project_id=PROJECT, idempotency_key="k", **extra) == "project_scope_mismatch"
    bridge.pool.AppendToTimeline.assert_not_called()
    bridge.scene.timeline.AddMarker.assert_not_called()


def test_timeline_prefix_policy_blocks_writes_on_real_timelines(bridge, tmp_path):
    bridge.scene.timeline.GetName.return_value = "Timeline 1"
    arguments = dict(project_id=PROJECT, timeline_id=TIMELINE, frame=86500, color="Blue", name="Cut here",
                     expected_snapshot_id=snapshot(bridge), idempotency_key="marker")
    assert error_code(bridge.server, "add_marker", **arguments) == "invalid_operation"
    bridge.scene.timeline.AddMarker.assert_not_called()
    bridge.tools.config = make_config(tmp_path, prefix="")
    assert ok(bridge.server, "add_marker", **arguments)["status"] == "verified"


# ------------------------------------------------------------ editing

def test_plan_then_execute_append_is_verified_and_idempotent(bridge):
    proposed = append_plan(bridge)
    assert proposed["record_end_frame"] == 86736
    bridge.pool.AppendToTimeline.assert_not_called()
    arguments = dict(project_id=PROJECT, timeline_id=TIMELINE, plan_id=proposed["plan_id"], idempotency_key="append-1")
    first = ok(bridge.server, "execute_append_clip", **arguments)
    assert first["status"] == "verified" and first["replayed"] is False
    assert first["result"]["item"]["start_frame"] == 86640
    assert ok(bridge.server, "execute_append_clip", **arguments)["replayed"] is True
    bridge.pool.AppendToTimeline.assert_called_once()


def test_execute_needs_a_plan_issued_by_this_server_for_that_timeline(bridge):
    assert error_code(bridge.server, "execute_append_clip", project_id=PROJECT, timeline_id=TIMELINE,
                      plan_id="forged", idempotency_key="k") == "invalid_operation"
    proposed = append_plan(bridge)
    assert error_code(bridge.server, "execute_append_clip", project_id=PROJECT, timeline_id="timeline-2",
                      plan_id=proposed["plan_id"], idempotency_key="k") == "project_scope_mismatch"
    bridge.pool.AppendToTimeline.assert_not_called()


def test_invalid_plan_is_reported_as_tool_error(bridge):
    assert error_code(bridge.server, "plan_append_clip", project_id=PROJECT, timeline_id=TIMELINE, media_id="media-1",
                      source_in_frame=72, source_out_frame=168, track_type="video", track_index=1,
                      record_frame=86410, expected_snapshot_id=snapshot(bridge)) == "invalid_operation"


@pytest.mark.parametrize("changes", [{"frame": True}, {"frame": 1.5}, {"color": "Magenta"}, {"surprise": 1},
                                     {"name": ""}, {"note": "x" * 5000}])
def test_bad_arguments_never_reach_resolve(bridge, changes):
    arguments = dict(project_id=PROJECT, timeline_id=TIMELINE, frame=86500, color="Blue", name="n",
                     expected_snapshot_id="s", idempotency_key="k") | changes
    assert error_code(bridge.server, "add_marker", **arguments) == "invalid_arguments"
    bridge.connect.assert_not_called()


def test_missing_required_argument(bridge):
    assert error_code(bridge.server, "read_timeline", project_id=PROJECT) == "invalid_arguments"


def test_marker_then_compare_snapshot_reports_the_diff(bridge):
    before = snapshot(bridge)
    result = ok(bridge.server, "add_marker", project_id=PROJECT, timeline_id=TIMELINE, frame=86520, color="Green",
                name="Chorus", note="from MCP", expected_snapshot_id=before, idempotency_key="marker-1")
    assert result["result"]["marker"]["offset_frame"] == 120
    comparison = ok(bridge.server, "compare_snapshot", project_id=PROJECT, timeline_id=TIMELINE, since_snapshot_id=before)
    assert comparison["matches"] is False
    assert [m["name"] for m in comparison["diff"]["markers"]["added"]] == ["Chorus"]
    assert comparison["diff"]["clips"] == {"added": [], "removed": [], "changed": []}
    unknown = ok(bridge.server, "compare_snapshot", project_id=PROJECT, timeline_id=TIMELINE, since_snapshot_id="timeline-v2-old")
    assert unknown["diff"] is None and "not read in this server session" in unknown["note"]


def test_failed_write_blocks_retry_and_is_visible_in_journal(bridge):
    proposed = append_plan(bridge)
    bridge.pool.AppendToTimeline.side_effect = RuntimeError("connection lost")
    arguments = dict(project_id=PROJECT, timeline_id=TIMELINE, plan_id=proposed["plan_id"], idempotency_key="append-x")
    assert error_code(bridge.server, "execute_append_clip", **arguments) == "resolve_write_failed"
    assert error_code(bridge.server, "execute_append_clip", **arguments) == "operation_uncertain"
    bridge.pool.AppendToTimeline.assert_called_once()
    listing = ok(bridge.server, "read_journal", limit=5)["operations"]
    assert listing[0]["status"] == "failed" and listing[0]["idempotency_key"] == "append-x"
    record = ok(bridge.server, "read_journal", operation_id=listing[0]["operation_id"])
    assert record["parameters"]["plan_id"] == proposed["plan_id"]
    assert error_code(bridge.server, "read_journal", operation_id="nope") == "invalid_operation"


def test_select_timeline_and_track_creation_are_journaled(bridge):
    assert ok(bridge.server, "select_timeline", project_id=PROJECT, timeline_id=TIMELINE,
              idempotency_key="select")["result"]["selected_timeline_id"] == TIMELINE
    count = [1]
    bridge.scene.timeline.GetTrackCount.side_effect = lambda kind: count[0] if kind == "video" else 1
    bridge.scene.timeline.AddTrack = Mock(side_effect=lambda kind: count.__setitem__(0, count[0] + 1) or True)
    bridge.scene.timeline.GetItemListInTrack.side_effect = lambda kind, index: [bridge.scene.item] if (kind, index) == ("video", 1) else []
    assert ok(bridge.server, "ensure_video_track", project_id=PROJECT, timeline_id=TIMELINE, index=2,
              idempotency_key="v2")["result"]["video_tracks"] == 2


def test_create_test_timeline_name_is_checked_before_connecting(bridge):
    assert error_code(bridge.server, "create_test_timeline", project_id=PROJECT, name="Client cut",
                      idempotency_key="k") == "invalid_arguments"
    bridge.connect.assert_not_called()


def test_search_media_returns_all_candidates(bridge):
    found = ok(bridge.server, "search_media", project_id=PROJECT, query="interview")
    assert found["count"] == 1 and found["matches"][0]["id"] == "media-1"
    assert ok(bridge.server, "search_media", project_id=PROJECT, query="interview", exact=True)["count"] == 0
    inventory = ok(bridge.server, "read_media", project_id=PROJECT)
    assert inventory["items"][0]["source_path"] == "/fake/Interview_A.mov"


def test_convert_time_is_exact_and_explicit(bridge):
    assert ok(bridge.server, "convert_time", rate="29.97", timecode="01:00:00:00")["frame"] == 108000
    df = ok(bridge.server, "convert_time", rate="29.97 DF", frame=1800)
    assert df["timecode"] == "00:01:00;02" and df["seconds_exact"] == "3003/50"
    assert error_code(bridge.server, "convert_time", rate="24", seconds="0.01") == "invalid_timecode"
    assert ok(bridge.server, "convert_time", rate="24", seconds="0.01", rounding="nearest")["frame"] == 0
    assert error_code(bridge.server, "convert_time", rate="24", frame=1, timecode="00:00:00:01") == "invalid_arguments"
    bridge.connect.assert_not_called()


# ------------------------------------------------------------ resilience

def test_unavailable_resolve_is_a_tool_error_and_reconnects_later(bridge):
    bridge.connect.side_effect = [ResolveUnavailableError("Resolve is not running."), bridge.scene.adapter]
    assert error_code(bridge.server, "resolve_status") == "resolve_unavailable"
    assert ok(bridge.server, "resolve_status")["project"]["id"] == PROJECT
    assert bridge.connect.call_count == 2


def test_unexpected_exception_does_not_stop_the_server(bridge):
    bridge.scene.resolve.GetVersionString.side_effect = None
    bridge.tools.resolve_status = Mock(side_effect=KeyError("boom"))
    assert error_code(bridge.server, "resolve_status") == "internal_error"
    assert rpc(bridge.server, "ping")["result"] == {}


# ------------------------------------------------------------ Fusion

def test_lower_third_through_mcp(fusion, tmp_path):  # noqa: F811
    tools = BridgeTools(make_config(tmp_path), connect=lambda: fusion.scene.adapter,
                        journal=OperationJournal(tmp_path / "mcp-fusion.db"))
    server = MCPServer(tools, log=Mock())
    scope = {"project_id": PROJECT, "timeline_id": TIMELINE}
    assert ok(server, "list_fusion_compositions", clip_id="clip-1", **scope)["compositions"] == ["Composition 1"]
    graph = ok(server, "read_fusion_graph", clip_id="clip-1", comp_name="Composition 1", **scope)
    view = ok(server, "prepare_fusion_composition", clip_id="clip-1", comp_name="Composition 1",
              idempotency_key="prep", **scope)["result"]
    proposed = ok(server, "plan_lower_third", clip_id="clip-1", comp_name="Composition 1", text="Alex Example\nDirector",
                  expected_snapshot_id=fusion.scene.adapter.read_timeline().snapshot_id,
                  expected_graph_id=graph["graph_id"], fade_frames=10, **scope)
    fusion.comp.AddTool.assert_not_called()
    done = ok(server, "execute_lower_third", plan_id=proposed["plan_id"], idempotency_key="lt-1", **scope)
    assert done["status"] == "verified" and done["result"]["keyframes"][1] == {"frame": 10, "value": 1}
    assert ok(server, "execute_lower_third", plan_id=proposed["plan_id"], idempotency_key="lt-1", **scope)["replayed"]
    assert fusion.comp.AddTool.call_count == 3
    restored = ok(server, "restore_fusion_view", page=view["page"], timecode=view["timecode"], idempotency_key="restore", **scope)
    assert restored["status"] == "verified"


def test_lower_third_refused_on_non_test_timeline(fusion, tmp_path):  # noqa: F811
    fusion.scene.timeline.GetName.return_value = "Timeline 1"
    tools = BridgeTools(make_config(tmp_path, prefix=""), connect=lambda: fusion.scene.adapter,
                        journal=OperationJournal(tmp_path / "mcp-fusion.db"))
    server = MCPServer(tools, log=Mock())
    graph = ok(server, "read_fusion_graph", project_id=PROJECT, timeline_id=TIMELINE, clip_id="clip-1", comp_name="Composition 1")
    # The service's own AI_BRIDGE_TEST_ rule holds even when server policy is relaxed.
    assert error_code(server, "plan_lower_third", project_id=PROJECT, timeline_id=TIMELINE, clip_id="clip-1",
                      comp_name="Composition 1", text="x", expected_snapshot_id="s",
                      expected_graph_id=graph["graph_id"]) == "invalid_operation"


# ------------------------------------------------------------ MIDI piano roll

def midi_server(roll, tmp_path, roots):  # noqa: F811
    config = ServerConfig.from_dict({"allowed_projects": [{"id": PROJECT, "allow_writes": True}], "journal_path": "j.sqlite3",
                                     "midi_roots": roots}, base=tmp_path)
    tools = BridgeTools(config, connect=lambda: roll.scene.adapter, journal=OperationJournal(tmp_path / "mcp-midi.db"))
    return MCPServer(tools, log=Mock())


def test_read_midi_file_needs_a_configured_root(roll, tmp_path):  # noqa: F811
    server = midi_server(roll, tmp_path, [str(roll.path.parent)])
    info = ok(server, "read_midi_file", midi_path=str(roll.path))
    assert [t["name"] for t in info["tracks"]] == ["Conductor", "Right Hand", "Left Hand", "Drums"]
    assert info["tempo_map"][1]["bpm"] == 80 and info["time_signatures"][1]["numerator"] == 3
    assert error_code(midi_server(roll, tmp_path, []), "read_midi_file", midi_path=str(roll.path)) == "project_scope_mismatch"
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    assert error_code(midi_server(roll, tmp_path, [str(elsewhere)]), "read_midi_file", midi_path=str(roll.path)) == "project_scope_mismatch"
    link = elsewhere / "escape.mid"
    link.symlink_to(DEMO)
    assert error_code(midi_server(roll, tmp_path, [str(elsewhere)]), "read_midi_file", midi_path=str(link)) == "project_scope_mismatch"
    (elsewhere / "notes.txt").write_text("x")
    assert error_code(midi_server(roll, tmp_path, [str(elsewhere)]), "read_midi_file", midi_path=str(elsewhere / "notes.txt")) == "invalid_operation"


def test_midi_piano_roll_through_mcp(roll, tmp_path):  # noqa: F811
    server = midi_server(roll, tmp_path, [str(roll.path.parent)])
    scope = {"project_id": PROJECT, "timeline_id": TIMELINE}
    graph = ok(server, "read_fusion_graph", clip_id="clip-1", comp_name="Composition 1", **scope)
    proposed = ok(server, "plan_midi_piano_roll", clip_id="clip-1", comp_name="Composition 1", midi_path=str(roll.path),
                  expected_snapshot_id=roll.scene.adapter.read_timeline().snapshot_id, expected_graph_id=graph["graph_id"],
                  track=2, bar_markers=True, **scope)
    assert proposed["layout"]["note_count"] == 11 and proposed["caps"]["nodes"] == 256
    assert [m["timeline_frame"] for m in proposed["layout"]["bar_markers"]] == [86400, 86458, 86515, 86569]
    roll.comp.AddTool.assert_not_called()
    done = ok(server, "execute_midi_piano_roll", plan_id=proposed["plan_id"], idempotency_key="roll-1", **scope)
    assert done["status"] == "verified" and done["result"]["added_nodes"] == proposed["layout"]["node_count"]
    calls = roll.comp.AddTool.call_count
    assert ok(server, "execute_midi_piano_roll", plan_id=proposed["plan_id"], idempotency_key="roll-1", **scope)["replayed"]
    assert roll.comp.AddTool.call_count == calls
    assert error_code(server, "execute_lower_third", plan_id=proposed["plan_id"], idempotency_key="wrong-kind", **scope) == "invalid_operation"


def test_midi_piano_roll_over_cap_and_bad_arguments(roll, tmp_path):  # noqa: F811
    from test_midi_roll import scale, PPQ
    server = midi_server(roll, tmp_path, [str(roll.path.parent)])
    scope = {"project_id": PROJECT, "timeline_id": TIMELINE, "clip_id": "clip-1", "comp_name": "Composition 1",
             "midi_path": str(roll.path), "expected_snapshot_id": roll.scene.adapter.read_timeline().snapshot_id,
             "expected_graph_id": ok(server, "read_fusion_graph", project_id=PROJECT, timeline_id=TIMELINE,
                                     clip_id="clip-1", comp_name="Composition 1")["graph_id"]}
    assert error_code(server, "plan_midi_piano_roll", **scope, seconds_visible=30) == "invalid_arguments"
    assert error_code(server, "plan_midi_piano_roll", **scope, channel=True) == "invalid_arguments"
    roll.path.write_bytes(scale(400, spacing_ticks=PPQ // 8))
    is_error, payload = call(server, "plan_midi_piano_roll", **scope)
    assert is_error and payload["error"]["code"] == "roll_too_large" and "Fusion nodes" in payload["error"]["message"]
    roll.comp.AddTool.assert_not_called()


# ------------------------------------------------------------ config and validator

def test_config_resolves_relative_journal_and_rejects_bad_shapes(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"allowed_projects": [{"id": "p", "name": "P", "allow_writes": True}],
                                "journal_path": "../workspace/ops.sqlite3"}), encoding="utf-8")
    config = ServerConfig.load(path)
    assert config.journal_path == (tmp_path.parent / "workspace" / "ops.sqlite3").resolve()
    assert config.write_timeline_prefix == "AI_BRIDGE_TEST_" and config.allowed_projects["p"]["allow_writes"]
    for raw in ({"allowed_projects": "p", "journal_path": "j"},
                {"allowed_projects": [{"id": "p"}, {"id": "p"}], "journal_path": "j"},
                {"allowed_projects": [{"id": "p", "allow_writes": "yes"}], "journal_path": "j"},
                {"allowed_projects": [{"id": "p"}], "journal_path": ""},
                {"allowed_projects": [{"id": "p"}], "backup_dir": 3},
                {"allowed_projects": [{"id": "p"}], "calibrated_builds": "21.0.0.48"},
                {"allowed_projects": [{"id": "p"}], "journal_path": "j", "run_scripts": True},
                {"allowed_projects": [{"id": "p"}], "journal_path": "j", "midi_roots": "/tmp"}):
        with pytest.raises(ConfigError):
            ServerConfig.from_dict(raw, base=tmp_path)
    with pytest.raises(ConfigError):
        ServerConfig.load(tmp_path / "missing.json")


def test_unlisted_project_defaults_to_read_only(tmp_path):
    config = ServerConfig.from_dict({"allowed_projects": [{"id": "p"}], "journal_path": "j"}, base=tmp_path)
    assert config.allowed_projects["p"] == {"name": "", "allow_writes": False}


@pytest.mark.parametrize("schema,value", [({"type": "integer"}, True), ({"type": "number"}, float("inf")),
                                          ({"type": "string", "pattern": "^a"}, "b"), ({"type": "boolean"}, 0)])
def test_validator_is_strict(schema, value):
    with pytest.raises(ArgumentError):
        validate(schema, value)


def test_stdio_process_keeps_stdout_clean(tmp_path):
    """Real subprocess over pipes. No tool here connects to Resolve."""
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"allowed_projects": [{"id": PROJECT}], "journal_path": "j.sqlite3"}), encoding="utf-8")
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "convert_time", "arguments": {"rate": "24", "frame": 48}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_media", "arguments": {"project_id": "client"}}}]
    # Point module discovery at an empty directory so nothing native could load.
    env = {**os.environ, "RESOLVE_SCRIPT_API": str(tmp_path / "no-resolve")}
    completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "mcp_server.py"), "--config", str(config)],
                               input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True,
                               text=True, timeout=30, env=env)
    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3, 4]
    assert len(responses[1]["result"]["tools"]) == 25
    assert responses[2]["result"]["structuredContent"]["timecode"] == "00:00:02:00"
    assert responses[3]["result"]["isError"] is True


def test_missing_config_exits_with_message(tmp_path):
    completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "mcp_server.py"), "--config", str(tmp_path / "none.json")],
                               input="", capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2 and "Cannot read MCP config" in completed.stderr and completed.stdout == ""
