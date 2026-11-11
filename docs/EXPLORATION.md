# Resolve bridge exploration — 30 September 2026

This records the core bridge milestone. The later [2 October VFX report](FUSION.md)
documents the implemented Fusion recipe and supersedes the Fusion next steps below.

## Result

The core workflow now runs against the user's authorized test project. It can read
state, inventory/search media, organize bins, plan an exact insertion, execute it,
verify the result, reject stale state, and avoid duplicate execution. No model or
network transport is involved yet.

## Verified evidence

- **194 unit tests passed.** Three live read integration tests also passed.
- **Ten live insertion/marker trials passed.** Each added one 120-frame clip on V2
  from source `[90, 210)`, then a marker. Repeating each exact request made no
  additional edit. All ten attempts to use the pre-insertion snapshot were rejected.
- The live source was a 910-frame gameplay capture at `30000/1001` fps. The successful
  fixture is `AI_BRIDGE_TEST_20260930_VERIFIED` in `AI Bridge / 90 Tests`.
- The original Timeline 1 was restored and its normalized snapshot stayed equal
  to the pre-test snapshot. Organization changed bin membership, not timeline cuts.
- Source paths stayed unchanged. Three source entries are in 01 Footage; the
  original timeline is in 05 Timelines.
- The project was saved and a 188,344-byte DRP exported. The ZIP archive passed an
  integrity check. Restoration has not been tested.

The detailed live reports and journal are not part of this repository. Failed trials
were retained rather than overwritten or retried.

## API findings that changed the implementation

### Inactive timeline track flags are misleading

The empty test timeline reported all tracks disabled while Timeline 1 was active.
Selecting the test timeline made the same tracks report enabled. Therefore the
bridge requires the requested timeline to be active for full edit-state reads.
Read-only planning never switches timelines. Test setup changes selection explicitly
and records/restores it.

### The installed append example disagrees with this build

The installed `7_add_subclips_to_timeline.py` describes source 0–23 as 24 frames.
Actual Resolve 21.0.0.48 inserts yielded:

| Native source start | Native source end | Observed duration | Source end getter |
| ---: | ---: | ---: | ---: |
| 0 | 23 | 23 | 23 |
| 0 | 24 | 24 | 24 |
| 90 | 209 | 119 | 209 |
| 90 | 210 | 120 | 210 |
| 100 | 199 | 99 | 199 |
| 100 | 200 | 100 | 200 |
| 900 | 909 | 9 | 909 |
| 900 | 910 | 10 | 909 |

The source contains 910 frames. The append API uses an exclusive end in these
observations, while the source end getter clamps at EOF. The initial inclusive-end
implementation was caught by read-after-write verification and stopped after one
119-frame insertion. No automatic rollback or second edit followed.

The current implementation uses this calibrated behavior and rejects other builds.
It still verifies every source/destination and duration after the native call.

### Mixed rates need an explicit mapping policy

The timeline is 29.97 fps; one source is 30 fps. Its original timeline duration is
9,144 frames while its source frame count is 9,154. Those domains cannot be treated
as interchangeable. Initial insertion supports matching rates only.

### Sandbox connection failure is not proof that Resolve is closed

The native library loaded inside the Codex sandbox, but `scriptapp("Resolve")`
returned None. The same command outside the sandbox connected successfully. Error
messages must not assert that the application is closed based on None alone.

## Safety limits

Project/timeline IDs scope mutations. The write journal persists intent before an
API call and serializes bridge writes. Content snapshots cover only normalized
edit-relevant fields; they do not lock the GUI or cover the entire project. External
changes can still race calls, so failures and post-write mismatches require inspection.

No deletion, ripple edit, original-file modification, relink, playback, Fusion
mutation, render, HTTP server, OpenAI controller, or MCP transport was implemented.
The full conversational v0.1 acceptance sequence therefore remains incomplete.

## Next work, essentials first

1. Add deterministic generated media fixtures at 24 and 29.97 fps and test additional
   frame boundaries, drop-frame timelines, audio insertions, and failures on isolated
   test timelines. The current live insertion evidence covers one 29.97 fps source.
2. Extend project manifests to inventory filesystem availability and duplicates,
   with explicit dry-run import/consolidation plans. Never conflate bin moves with
   file moves or guess a source when multiple candidates match.
3. Expand snapshot coverage only as new operations require it; add markers/source
   state for new operations and avoid claiming a universal project revision.
4. Build a separate Fusion adapter. The installed Resolve docs expose
   `GetFusionCompCount`, `GetFusionCompByIndex`, and `GetFusionCompNameList` on timeline
   items. Node/parameter APIs need their own documentation review and live tests.
5. First useful VFX candidate: a reusable Text+ lower third in a disposable
   composition, with explicit text/position parameters, normalized graph reads,
   bounded operations, and graph/value verification after each mutation. This is
   a proposal, not an implemented or tested feature.

## Documentation consulted

Installed Resolve Scripting README (updated 8 May 2026), native module wrapper,
project-information example, and subclip-append example under:

```text
/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/
```

Drop-frame numbering and frame conversion were also cross-checked with
[Apple Technical Note TN2310](https://developer.apple.com/library/archive/technotes/tn2310/_index.html).
