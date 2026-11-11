# Reference: library, scripts and contracts

Install, configuration and the MCP setup are in the [README](../README.md). This page is the
developer reference for the Python library and the checkout's helper scripts.

A local Python bridge for reading Resolve state, organizing a project, and making
small, verified edits. Native Resolve objects stay inside the adapter modules.
There is no arbitrary Python, shell, or scripting-expression tool for a model.

## Current status

Tested on **DaVinci Resolve Studio 21.0.0.48**, Python 3.13.9, macOS arm64.

- Normalized project, timeline, track, clip, marker, and Media Pool reads.
- Source inventory with stable IDs, bin hierarchy, filenames, source paths, rates,
  frame counts, source origins, and online status.
- Exact/partial Unicode-normalized, case-insensitive search. Multiple candidates
  are returned explicitly; unique selection rejects ambiguity.
- Frame/timecode conversions, exact elapsed-time arithmetic, explicit rounding,
  and timeline start offsets.
- Snapshot checks over normalized edit-relevant state.
- Scoped project setup, Media Pool bin organization, project save and DRP backup.
- Read-only append plans; verified clip insertion and markers; SQLite journal and
  at-most-once execution attempts.
- Fusion graph reads and fingerprints, composition creation, and a verified
  animated Text+ lower-third recipe in isolated test timelines.
- Three live read tests and ten live editing trials passed on that build. The VFX
  demo passed graph checks and exported-frame inspection.

This is the core bridge milestone. The full v0.1 conversational workflow is not
complete: no HTTP service or render queue exists yet. A stdio MCP server exposes
the bounded operations to an agent; see [MCP](MCP.md), the [VFX report](FUSION.md)
and the [core bridge report](EXPLORATION.md).

## Fusion VFX

Read compositions or a graph with explicit IDs:

```bash
python scripts/inspect_fusion.py --project-id PROJECT_ID \
  --timeline-id TIMELINE_ID --clip-id CLIP_ID --comp-name 'Composition 1'
```

Preview an isolated 240-frame lower-third demo; add `--execute` to perform it:

```bash
python scripts/fusion_demo.py --project-id PROJECT_ID \
  --media-id SOURCE_MEDIA_ID --run-id UNIQUE_RUN_ID
```

The demo creates a Text+ title, connects it through Merge, and animates opacity
with four keyframes. It explicitly loads the clip on Fusion, then restores the
original timeline/page/playhead. Execution uses graph and timeline fingerprints,
readback, Undo grouping, and the shared journal. Only simple base compositions in
test timelines on a calibrated Resolve build are accepted. See [Fusion commands and limits](FUSION.md)
and [RECALIBRATION](RECALIBRATION.md).

## Read-only commands

From the checkout, using compatible 64-bit Python 3.10 or newer:

```bash
python scripts/inspect_resolve.py
python scripts/inspect_media.py
python scripts/inspect_media.py --query 'part of a clip name'
python scripts/inspect_media.py --query 'filename.mp4' --exact
```

The inspection commands do not launch Resolve, select timelines, move the playhead,
play media, or write the project. Success is JSON on stdout with exit code 0;
expected errors are typed JSON on stderr with exit code 1.

Resolve must already be open with a project. For external scripting, the installed
documentation targets Studio; enable Local scripting access if your edition
supports it. A sandboxed shell (Codex's command sandbox, for one) can block the
connection even while Resolve is running; the same scripts work outside it.

### Module loading

1. An explicit `RESOLVE_SCRIPT_API` selects its `Modules` directory.
2. Otherwise import `DaVinciResolveScript` from Python's current import path.
3. If absent, try the documented platform installation directory.

The installed macOS wrapper is:

```text
/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules/DaVinciResolveScript.py
```

Blackmagic's wrapper loads the native extension, honoring `RESOLVE_SCRIPT_LIB`,
then its default installation location:

```text
/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so
```

The wrapper replaces its module entry with that extension, so the reported module
path normally ends in `fusionscript.so`. `.env` files are not loaded automatically. Use a fresh Python process after changing
module locations. Windows/Linux defaults exist but have not been tested.

## Project setup and organization

Get the stable project ID from inspection. Both commands default to a preview:

```bash
python scripts/setup_project.py --project-id PROJECT_ID
python scripts/organize_project.py --project-id PROJECT_ID
```

To execute an explicit setup, add `--execute`. Use `--test-name AI_BRIDGE_TEST_NAME`
with a fresh name when another empty fixture is needed. To execute bin organization,
add `--execute --idempotency-key UNIQUE_REQUEST_KEY`.

Organization proposes moving root-bin items into known bins by media type. Unknown
types or ambiguous destinations remain unresolved. It does not reorganize items
already in other bins or make any filesystem changes. Every execution checks the
project ID and rereads the resulting state. See [project layout](PROJECT_LAYOUT.md).

The Python service accepts the exact original organization plan for idempotent
replay. Replanning creates a different request when the inventory has changed;
reusing its old key then fails explicitly instead of performing another move.

## Editing contract

`EditingService` requires explicit project and timeline IDs plus a journal. The
requested timeline must be active; planning never implicitly changes selection.
`plan_append_clip()` returns a frozen plan. `execute_append_clip()` accepts that
exact plan and a mandatory idempotency key.

- Positions are integer, absolute timeline frames. A timeline beginning at
  `01:00:00:00` at 29.97 NDF begins at frame **108000**, not zero.
- Source ranges in this bridge are **half-open `[in, out)`**.
- On the calibrated Resolve **21.0.0.48** build, `AppendToTimeline` also uses an
  exclusive source end. The installed example implies otherwise; eight live
  probes established the implemented convention. Unknown builds are rejected.
- Source end getters have different behavior at EOF: they report the requested
  exclusive end for tested trimmed clips, but clamp to the last source frame at
  EOF. Verification handles this observed distinction. Raw source end fields are
  not silently converted on general timeline reads.
- Insertion currently requires matching source/timeline rates, known source length,
  zero source origin, Online media, an existing unlocked/enabled track, and an
  empty destination range. Live insertion has been exercised at 29.97 fps; other
  rates still need live fixture coverage.
- Markers take an absolute frame; the adapter translates it to Resolve's timeline
  offset. Markers must lie within the current timeline range and cannot overwrite
  an existing marker at the same position.
- Every write is followed by a state reread. Clip verification checks exact media,
  track, source range, destination, duration, and preservation of prior items.
- A failed or interrupted request is never automatically retried or rolled back.
  Resolve may have partially applied it. Inspect the journal and live state first.

### Snapshot limits

Schema v2 hashes project/timeline IDs, names, frame rate, start/end, tracks and their
locks/enabled flags, clip IDs/media/ranges/source endpoints, and timeline markers.
Connection paths and version-reporting metadata do not affect the snapshot.

This is a content fingerprint of the fields read, not an atomic transaction or a
complete Resolve project revision. Grades, effects, transitions, clip-level markers,
retiming details, and Fusion graphs are not covered. A user can edit between the
last read and the API call. Snapshot validation plus read-after-write detects many
conflicts but cannot prevent all concurrent GUI changes.

**Resolve 21 returns misleading track-enable flags for inactive timelines.** Full
edit-state reads therefore require the active timeline. Listing timeline names/IDs
is available without switching. The live test runner journals explicit selection
and restores the original timeline at the end.

### Idempotency and journal

The journal (`resolve-bridge paths` shows where) records intent before any Resolve mutation, including
operation ID, request key, timestamp, type, exact parameters, status, result, and
error. Successful repeats return the stored result. A key reused with different
parameters is rejected. Failed/running records block automatic replay.

A separate SQLite lock database serializes bridge mutations sharing this journal,
including requests with different keys. All callers must use the same journal to
share that lock. Stored replay results describe the original verified operation;
they do not assert that the user has left its result unchanged since then.

## Timecode and elapsed seconds

All arithmetic lives in `timecode.py`. Required rates 23.976, 24, 25, 29.97, and 30
are covered, with 29.97/59.94 drop-frame support. Broadcast labels use exact rational
rates such as `30000/1001`.

- `timecode_to_frames()` / `frames_to_timecode()` use absolute labels within a
  24-hour day. NDF uses `:`, DF uses `;`. Skipped DF labels are rejected.
- `seconds_to_frames()` uses real elapsed seconds. Nonintegral results require an
  explicit `floor`, `ceil`, or `nearest` policy; default `exact` refuses rounding.
- `frames_to_seconds()` returns an exact `Fraction`.
- `timeline_offset_to_frame()` / `frame_to_timeline_offset()` handle start offsets.
- Negative positions and silent 24-hour wrapping are rejected. Conversion of rates
  requiring a three-digit frame field is deferred, although inventory can read them.

For example, one second of 29.97 timecode is 30 frames, while those frames last
1.001 elapsed seconds. Drop-frame rules were cross-checked against
[Apple's timecode technical note](https://developer.apple.com/library/archive/technotes/tn2310/_index.html).

## Tests and live evidence

```bash
python -m pytest -q
```

The default run skips the opt-in integration tests and two optional MIDI cross-checks. Unit
tests never connect to Resolve. If needed, install with `python -m pip install -e
'.[test]'` in a virtual environment.

Opt-in read-only integration tests:

```bash
RESOLVE_BRIDGE_LIVE=1 RESOLVE_BRIDGE_PROJECT_ID=PROJECT_ID python -m pytest tests/integration -q
```

For live mutations, first create a fresh, empty `AI_BRIDGE_TEST_` timeline. Keep
an original timeline active so its content can be compared after the run:

```bash
python scripts/smoke_test.py --project-id PROJECT_ID --timeline-id TEST_TIMELINE_ID \
  --media-id SOURCE_MEDIA_ID --run-id UNIQUE_RUN_ID --trials 10
```

That command previews the test. Add `--execute` to perform it. Each trial inserts
source frames `[90, 210)` on V2, adds a blue marker, replays both requests to confirm
no duplicate edit, and verifies stale-snapshot rejection. A nonempty fixture or
an existing run report is rejected. Failed records are retained for diagnosis.
Run live scripts with a bounded external timeout; after a timeout, inspect the
journal and selection before any new attempt.

Reports and the journal live in the state folder, outside the checkout.
No destructive delete, ripple, overwrite, relink, or render operation is implemented.

## Code layout and next step

```text
src/resolve_bridge/
  connection.py     Installed module discovery and connection
  _api.py           Internal native-call/value validation helpers
  models.py         Immutable normalized values
  timeline.py       Read adapter and active-timeline enumeration
  media.py          Media inventory and pure search
  timecode.py       Central frame/timecode/elapsed-time arithmetic
  snapshot.py       Normalized state fingerprints
  project.py        Scoped setup, bin organization, save/backup
  operations.py     Narrow append/marker plans and execution
  fusion.py         Graph reads, fingerprints, and bounded Text+ recipe
  verification.py   Pure post-write checks
  journal.py        Durable intent, idempotency, single-writer lock
  errors.py         Typed errors
```

Also: `config.py` (locations and config schema), `calibration.py` (the build pin),
`cli.py` (`resolve-bridge doctor/init/paths`), `mcp_server.py`, `midi.py`, `midi_roll.py`.
