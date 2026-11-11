# Recalibrating for a new Resolve build

The bridge ships calibrated for **Resolve Studio 21.0.0.48**. On any other build
the MCP server is read-only: every write tool returns `uncalibrated_build`.
This page explains why, and how to earn writes on your build.

## What the pin protects

Two write paths depend on behaviour that was measured, not documented.

1. **`AppendToTimeline` source end.** Blackmagic's installed example
   (`7_add_subclips_to_timeline.py`) implies an inclusive end frame. Eight live
   inserts on 21.0.0.48 showed an **exclusive** end. The bridge's `[in, out)`
   plans pass `out` straight through. If a build changes this, every append is
   one frame long or short.
2. **Fusion edits need the clip loaded.** On 21.0.0.48 a graph edited while
   detached reads back correctly and renders plain footage. The recipes
   therefore load the clip on the Fusion page first. Graph checks cannot see a
   regression here; only a rendered frame can.

Markers, tracks, timeline selection and backup did not need calibration, but the
server refuses them too on an unknown build, so that "read-only" means one thing.

Where it lives in the code: `src/resolve_bridge/calibration.py`
(`SHIPPED_BUILDS`), checked in `operations.py` (append), `fusion.py` (Fusion
writes) and `mcp_server.py` (all write tools). `resolve_status` reports
`resolve_version`, `calibrated_build`, `calibrated_builds` and `writes_available`.

## Before you start

- Use a **disposable project** with one online video clip of at least 910
  frames whose frame rate matches the project's timeline rate.
- `resolve-bridge doctor` passes except for the `resolve_build` warning.
- The project is in your config's `allowed_projects`.
- Note the project ID (`resolve-bridge doctor`) and the media ID:

```bash
python3 scripts/inspect_media.py --query 'part of the clip name'
```

The scripts run from a checkout of this repository and use the same config and
journal as the server (`resolve-bridge paths`).

## Step 1: probe the append boundary

This script calls `AppendToTimeline` directly, so it is not blocked by the pin.
It creates `AI_BRIDGE_TEST_<run-id>_CALIBRATION`, inserts eight ranges 300 frames
apart, restores your timeline selection and checks your original timeline is
unchanged. Preview first (no `--execute`), then:

```bash
python3 scripts/calibrate_append.py --project-id PROJECT_ID --media-id MEDIA_ID --run-id BUILD_NUMBER --execute
```

Use a fresh `--run-id` each time (letters, digits, `_`, `-`), for example
`21_0_1_5`. A repeated run ID is refused rather than repeated.

Compare each observation's `observed_item` with the 21.0.0.48 record
(also in [EXPLORATION.md](EXPLORATION.md)):

| Native start | Native end | Duration on 21.0.0.48 | Source end getter |
| ---: | ---: | ---: | ---: |
| 0 | 23 | 23 | 23 |
| 0 | 24 | 24 | 24 |
| 90 | 209 | 119 | 209 |
| 90 | 210 | 120 | 210 |
| 100 | 199 | 99 | 199 |
| 100 | 200 | 100 | 200 |
| 900 | 909 | 9 | 909 |
| 900 | 910 | 10 | 909 (clamped at end of file) |

**Pass:** every duration equals `end - start`, and the last row's getter is
clamped as shown (with a longer clip the last two rows are not at end of file
and will not clamp; that is fine).

**Fail:** any duration equals `end - start + 1`, or anything else differs.
Stop here. Your build treats the range differently and the bridge's append is
wrong for it. Do not add the build to `calibrated_builds`.

## Step 2: trust the build provisionally

Add your build string, exactly as `resolve_status` reports it, to your config:

```json
"calibrated_builds": ["21.0.1.5"]
```

## Step 3: verified edits through the normal path

Create an empty test timeline (`scripts/setup_project.py --project-id PROJECT_ID
--test-name AI_BRIDGE_TEST_<run-id>_SMOKE --execute`), keep your original timeline
active, then:

```bash
python3 scripts/smoke_test.py --project-id PROJECT_ID --timeline-id TEST_TIMELINE_ID --media-id MEDIA_ID --run-id BUILD_NUMBER-smoke --trials 3 --execute
```

**Pass:** every trial `verified`, every replay `replayed: true`, and the report
shows the original timeline's snapshot unchanged.

## Step 4: Fusion, with your eyes

```bash
python3 scripts/fusion_demo.py --project-id PROJECT_ID --media-id MEDIA_ID --run-id BUILD_NUMBER-fusion --execute
```

The command's `verified` covers the node graph only. Open the demo timeline in
Resolve and scrub across the clip: the title must fade in, hold and fade out
over the footage. If the graph verifies and the viewer shows plain footage,
Fusion writes are **not** calibrated on your build.

## Step 5: decide

| Result | Do |
|---|---|
| Steps 1, 3 and 4 pass | Keep the build in `calibrated_builds`. Restart the MCP server. `resolve_status` now shows `writes_available: true`. |
| Step 1 or 3 fails | Remove the build from `calibrated_builds`. Stay read-only. |
| Only step 4 fails | Remove the build. There is no per-feature switch; a build is calibrated for all writes or none. |

Keep the reports (`resolve-bridge paths` → `reports_dir`) and the journal. Never
delete `failed` or `running` journal rows to make a rerun possible: they are the
record of what Resolve may have half-done. Use a new run ID instead.

If you maintain the package and a build passes, add it to `SHIPPED_BUILDS` in
`calibration.py` along with the evidence.

## Not covered by this runbook

- Frame rates other than the one you tested. Live inserts were exercised at
  29.97 fps; mixed source/timeline rates are refused regardless of build.
- The MIDI piano-roll recipe, which has only run against fakes. Treat its first
  live run as an experiment on any build; see [MCP.md](MCP.md).
- Windows and Linux. Module discovery has defaults for both; nothing was run there.
