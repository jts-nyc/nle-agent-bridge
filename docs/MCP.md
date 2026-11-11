# MCP server: internals and live smoke test

`resolve_bridge.mcp_server` exposes the bridge's bounded operations to Claude Code
(or any MCP client) as typed tools over stdio. It adds no new kind of Resolve
write. Every tool is an existing read, plan, or journaled write from
`operations.py`, `project.py` or `fusion.py`. It provides no script, Lua,
expression, or generic parameter tool.

It has no third-party dependencies. The official `mcp` Python SDK would pull in
pydantic, starlette, uvicorn, httpx and roughly twenty more packages. The server
must run in the same Python that loads Resolve's native `fusionscript.so`, so it
implements the small part of the protocol it needs (stdio JSON-RPC 2.0, tools
only, protocol versions 2024-11-05 to 2025-11-25) in one module.

## Set it up

Install, config keys, environment variables and the client snippets for Claude
Code, Claude Desktop and Codex are in the [README](../README.md). In short:
`resolve-bridge doctor`, `resolve-bridge init`, add your project to
`allowed_projects`, register `resolve-bridge-mcp` with your client. `/mcp` in
Claude Code should then list 25 tools.

**Use one journal.** The journal's lock and replay protection only work when
every caller shares the same file. The helper scripts in `scripts/` read the same
config as the server, so they share it by default.

**Permissions (optional, Claude Code).** Claude Code asks before each MCP tool
call. You can pre-allow the read tools and keep the writes on prompt by adding
entries such as `mcp__resolve__resolve_status`, `mcp__resolve__read_timeline`
and `mcp__resolve__search_media` to `permissions.allow` in your settings.

The server connects to Resolve lazily, on the first tool that needs it.
Starting Claude Code with Resolve closed is harmless. Tools return a
`resolve_unavailable` error until Resolve is open, then reconnect on their own.

## Tools

Reads (`readOnlyHint`):

| Tool | What it does |
|---|---|
| `resolve_status` | Resolve build, whether it is calibrated and writes are available, the open project and whether it is allowlisted, active timeline, server policy. No arguments. |
| `list_timelines` | Names and IDs, marks the active one. No selection change. |
| `read_timeline` | Full normalized state of the active timeline plus `snapshot_id`. |
| `read_media` | Whole Media Pool inventory. |
| `search_media` | Name/filename search; returns every candidate. |
| `convert_time` | Exact timecode ↔ frame ↔ seconds for a rate, with explicit rounding. No Resolve call. |
| `compare_snapshot` | Re-read and compare with an earlier `snapshot_id`; itemized diff when that read happened in this session. |
| `read_journal` | One operation in full, or the latest attempts. |
| `plan_append_clip` | Validates an insertion; returns a `plan_id`. |
| `list_fusion_compositions`, `read_fusion_graph` | Fusion reads; work on inactive timelines. |
| `plan_lower_third` | Validates the Text+ recipe; returns a `plan_id`. Needs a writable project, because the plan checks write scope. |
| `read_midi_file` | Parses a `.mid` inside `midi_roots`: tracks (1-based), channels, pitch range, tempo map, time signatures. No Resolve call. |
| `plan_midi_piano_roll` | Validates the piano-roll recipe and returns a `plan_id` plus a layout summary (note, rectangle, chunk and node counts, first onsets, bar-marker frames). Over the 256-node cap it fails with `roll_too_large` and what to narrow. Needs a writable project. |

Writes (journaled, each takes an `idempotency_key`):

| Tool | What it does |
|---|---|
| `execute_append_clip` | Executes a `plan_id` from `plan_append_clip`, verifies the exact result. |
| `add_marker` | One marker at an absolute frame; never overwrites one. |
| `ensure_video_track` | Adds video tracks up to an index (max 16). |
| `select_timeline` | Makes a timeline active (UI state); returns the previous one. |
| `create_test_timeline` | Empty `AI_BRIDGE_TEST_*` timeline in `AI Bridge / 90 Tests`. |
| `save_and_backup` | Save plus `.drp` export with archive check. |
| `create_fusion_composition` | First composition on a clip that has none. |
| `prepare_fusion_composition` | Loads the clip on the Fusion page (needed before edits render); returns the previous page and timecode. |
| `execute_lower_third` | Executes a `plan_id` from `plan_lower_third`. |
| `execute_midi_piano_roll` | Executes a `plan_id` from `plan_midi_piano_roll`, including its bar markers. |
| `restore_fusion_view` | Returns to a saved page and timecode. |

## Safety model

- **Allowlist plus open project.** Every project read and every write names a
  `project_id`. It must be in the config allowlist and be the project open in
  Resolve, or the call fails before any Resolve call. `resolve_status` shows the
  open project's name even if it is not allowlisted, but nothing inside it.
- **Build pin.** On a Resolve build that is not calibrated every write tool
  returns `uncalibrated_build`; see [RECALIBRATION](RECALIBRATION.md).
- **Writes are opt-in per project** (`allow_writes`), and timeline writes are
  limited to `AI_BRIDGE_TEST_` timelines by default. The Fusion recipe keeps its
  own test-timeline rule even if you relax the server's prefix.
- **Plan then execute.** `execute_*` tools take only a `plan_id` the server
  issued in this session. Claude cannot hand in an edited plan. The services
  revalidate the plan, the snapshot and the media fingerprint immediately before
  writing anyway. Plans do not survive a server restart; plan again.
- **Unchanged write path.** Every write goes through `OperationJournal.execute`:
  intent is recorded before Resolve is touched, there is one writer at a time,
  the same key and request replay the stored result, the same key with a
  different request is refused, and a failed or interrupted attempt blocks
  automatic retry. Each write is followed by a reread and exact verification.
- **MIDI files only from `midi_roots`.** The path is resolved before the check, so
  a symlink can't point outside. Files must end `.mid`/`.midi`, be at most 2 MB, and
  parse as a Standard MIDI File. The plan records the file's SHA-256, and execution
  refuses if the file changed.
- **Strict arguments.** Unknown fields, booleans passed as integers, out-of-range
  numbers and bad enums are rejected before Resolve is contacted.
- **Clean protocol stream.** At start-up the server moves file descriptor 1 to
  stderr and keeps a private copy for protocol output. Resolve's native wrapper
  prints to stdout (the `CopySettings` NameError, for one), and that output would
  otherwise corrupt the JSON-RPC stream.
- **What it cannot protect against.** Everything the README's snapshot limits
  say. A GUI edit can race a call. Snapshots don't cover grades, effects or
  Fusion. Undo groups don't roll back. A failed write may be partly applied.
  The server's instructions tell Claude to stop and inspect after any failed
  write, not to retry.

Native calls run on the server's single thread. If Resolve hangs mid-call, the
tool call hangs, and Claude Code's MCP timeout ends it, not the server. After a
timeout, run `read_journal` before anything else. A `running` record means the
outcome is unknown.

## Live smoke test (five steps, Resolve open)

Run this with a disposable, allowlisted, writable project open, its original
timeline (called Timeline 1 below) active, and Preferences ›
System › General › External scripting set to Local. Each step is a prompt you
can give Claude in a session where the `resolve` server is connected. Approve
the write tools when asked.

1. **Connect.** "Call `resolve_status`."
   Expect `calibrated_build: true`, the project allowlisted with writes
   allowed, and your active timeline.

2. **Read without touching.** "List the timelines, read Timeline 1, and search
   the media for part of a clip's name."
   Write down the `snapshot_id` it returns, because step 5 compares against it. Note a media ID with at least 210 frames
   and the same 29.97 rate as the timeline.

3. **Prove the guard rails.** "Try to add a marker on Timeline 1 at its start
   frame plus 30." Expect a refusal from the timeline-prefix policy and nothing
   in Resolve. Then: "Create a test timeline named `AI_BRIDGE_TEST_20261005_MCP`,
   select it, and read it."

4. **One verified edit, twice.** "Plan appending that media's source frames
   [90, 210) to video track 1 at the timeline's start frame, execute it with key
   `mcp-smoke-append`, then execute the same plan with the same key again. Add a
   Blue marker named `MCP smoke` 60 frames after the start with key
   `mcp-smoke-marker`. Then compare against the snapshot from before the append."
   Expect the first execute `verified` and the second `replayed: true`, with one
   120-frame clip on the timeline, not two. The diff should show exactly one
   added clip and one added marker, plus the timeline end moving out by 120 frames.

5. **Restore and back up.** "Select Timeline 1 again, read it, and confirm its
   snapshot matches step 2. Then save and back up with label `20261005-mcp` and
   key `mcp-smoke-backup`, and show the last 10 journal entries."
   Expect a matching snapshot, a new `.drp` under the journal's
   `project_backups/`, and every smoke-test journal row `verified`.

Optional sixth step for the VFX path, on the new test timeline: "Select the test
timeline. Create a Fusion composition on its clip, prepare it, read the graph,
plan a lower third reading `MCP SMOKE / Text+`, execute it, then restore the
view and reselect Timeline 1." The graph check isn't visual proof. Look
at the frame in Resolve (or export a still) to confirm the title renders.

### Piano-roll smoke test (first live run of the recipe)

The MIDI piano roll has only run against fakes. This is its first contact with
Resolve. [FUSION.md](FUSION.md#assumptions-the-live-run-must-confirm) lists the six
Fusion details it assumes. If a step fails, stop, ask for `read_journal` on the
failed operation, and keep the error message: it names the missing or unexpected
nodes and edges.

0. **Config.** Add the folder holding the demo file (`tests/fixtures` in a
   checkout) to `midi_roots` in your config, as an absolute path, then restart
   the MCP server (it reads its config at start-up).

1. **Parse.** "Call `read_midi_file` on `/absolute/path/to/tests/fixtures/demo-two-hands.mid`."
   Expect four tracks (Conductor, Right Hand, Left Hand, Drums), 100 BPM then 80
   BPM from 4.8 s, and 4/4 then 3/4.

2. **A clip to draw on.** "Create a test timeline named
   `AI_BRIDGE_TEST_20261005_MIDI`, select it, read it, and append 300 frames of a
   video clip (source [90, 390), or [0, 300) if it is shorter) to video
   track 1 at the start frame with key `midi-smoke-append`. Read it again and give
   me the clip ID."

3. **Load the composition.** "Create a Fusion composition on that clip with key
   `midi-smoke-comp`, prepare it with key `midi-smoke-prep`, and read its graph."
   Note the `page` and `timecode` that prepare returns.

4. **Plan.** "Plan a MIDI piano roll on that composition from the demo file with
   bar markers on." At 29.97 fps expect 22 notes, 26 rectangles in 2 chunks,
   41 nodes, 25 rows (G2 to G4, MIDI 43 to 67), and bar markers at the
   timeline start plus 0, 72, 144 and 211 frames. Nothing in Resolve changes.

5. **Build, twice.** "Execute it with key `midi-smoke-roll`, then again with the
   same key." Expect `verified` with `onset_checks` whose `left_edge_x` values are
   0.25, then `replayed: true` and no new nodes.

6. **Look.** In Resolve, scrub the Fusion viewer (or the Edit viewer) to the Bar 3
   marker, 4.8 s in. The right hand's E4 should start on the white line, and bars
   left of the line should be brighter. Check that the rows are evenly spaced and
   don't overlap. If they look squashed or overlap, that is assumption 1 in
   FUSION.md, a one-line change. The result's `visual_verification:
   not_performed` stays true until you've looked.

7. **Restore.** "Restore the Fusion view to the saved page and timecode, select
   Timeline 1, read it, and confirm its snapshot matches step 2 of the main smoke
   test."

Try one over-cap request too, which should change nothing: plan a roll from a
dense MIDI file. Expect `roll_too_large` with the node count against the 256 cap
and what to narrow.

If step 1 returns `resolve_unavailable` while Resolve is open, run
`resolve-bridge doctor` in a terminal with the same interpreter. If that passes
and the server doesn't, the MCP launch environment differs. Pass
`RESOLVE_SCRIPT_API` in the server's `env` when registering.

## Tests

`tests/unit/test_mcp_server.py` drives the server through `handle()` and a real
subprocess over pipes, using the same fakes as the rest of the suite. It never
connects to Resolve. The subprocess test sets `RESOLVE_SCRIPT_API` to an empty
directory so nothing native can load. It covers the handshake, tool schemas,
allowlist and read-only refusals, the prefix policy, the plan registry,
append/marker/Fusion flows with idempotent replay, the MIDI tools (root
policy including symlinks, the roll through MCP, the cap error), failed-write blocking,
journal inspection, the snapshot diff, argument validation, reconnecting after
Resolve is unavailable, and stdout hygiene.
