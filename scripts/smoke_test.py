#!/usr/bin/env python
"""Explicit opt-in live tests in a dedicated AI_BRIDGE_TEST_ timeline.

Leaves verified test clips/markers for inspection. Never deletes content, renders,
or plays media. The current project's stable ID is required, not inferred.
"""

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge import calibration
from resolve_bridge.config import ServerConfig
from resolve_bridge.connection import connect
from resolve_bridge.errors import InvalidOperationError, ResolveBridgeError, StaleSnapshotError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.operations import EditingService
from resolve_bridge.project import ProjectSetup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--timeline-id", required=True)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trials", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.run_id) is None:
        parser.error("--run-id requires 1–64 letters, digits, underscores, or hyphens")
    config = ServerConfig.resolve()
    calibration.register(config.calibrated_builds)
    report_path = config.reports_dir() / f"{args.run_id}.json"
    report = None
    setup = None
    selection_changed = False
    try:
        adapter = connect()
        original = adapter.read_timeline()
        target = next((timeline for timeline in adapter.list_timelines() if timeline["id"] == args.timeline_id), None)
        if original.project.id != args.project_id or target is None or not target["name"].startswith("AI_BRIDGE_TEST_"):
            raise InvalidOperationError("Smoke tests require the explicitly scoped project and an AI_BRIDGE_TEST_ timeline.")
        if original.timeline.id == args.timeline_id:
            raise InvalidOperationError("Select the original timeline first so its unchanged state can be checked after tests.")
        if not args.execute:
            print(json.dumps({"executed": False, "project": original.project.name, "timeline": target["name"],
                              "trials": args.trials, "media_id": args.media_id,
                              "effects": "Ensure V2; insert disjoint 120-frame source ranges and blue markers; verify and retry each same request."}, indent=2))
            return 0
        if report_path.exists():
            raise InvalidOperationError(f"Run ID already has a report at {report_path}; inspect it instead of automatically retrying.")
        journal = OperationJournal(config.journal_path)
        service = EditingService(adapter, args.project_id, args.timeline_id, journal)
        setup = ProjectSetup(adapter, args.project_id, journal)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {"status": "running", "run_id": args.run_id, "project_id": args.project_id,
                  "timeline_id": args.timeline_id, "original_snapshot": original.snapshot_id, "trials": []}

        def checkpoint():
            report_path.write_text(json.dumps(report, indent=2) + "\n")

        checkpoint()
        report["selection"] = setup.select_timeline(args.timeline_id, idempotency_key=args.run_id + ":select")
        selection_changed = True
        checkpoint()
        target_state = service.read()
        if any(track.items for track in target_state.timeline.tracks) or target_state.timeline.markers:
            raise InvalidOperationError("Live fixture must be empty at the start; create a new test timeline for another run.")
        report["track_setup"] = service.ensure_video_track(2, idempotency_key=args.run_id + ":v2")
        checkpoint()
        for index in range(args.trials):
            before = service.read()
            record = before.timeline.start_frame + 360 + index * 180
            plan = service.plan_append_clip(media_id=args.media_id, source_in_frame=90, source_out_frame=210,
                track_type="video", track_index=2, record_frame=record, expected_snapshot_id=before.snapshot_id)
            append_key = f"{args.run_id}:append:{index}"
            insertion = service.execute_append_clip(plan, idempotency_key=append_key)
            state_after_insert = service.read()
            replay = service.execute_append_clip(plan, idempotency_key=append_key)
            if not replay["replayed"] or service.read().snapshot_id != state_after_insert.snapshot_id:
                raise InvalidOperationError("Repeated append changed state.")
            # The pre-insertion snapshot must now be stale, without a write.
            try:
                service.plan_append_clip(media_id=args.media_id, source_in_frame=90, source_out_frame=210,
                    track_type="video", track_index=2, record_frame=record + 120, expected_snapshot_id=before.snapshot_id)
            except StaleSnapshotError:
                stale_rejected = True
            else:
                raise InvalidOperationError("Stale snapshot was unexpectedly accepted.")
            marker_arguments = {"frame": record + 30, "color": "Blue", "name": f"AI TEST {index + 1:02}",
                "note": "Verified insertion and idempotency test", "expected_snapshot_id": state_after_insert.snapshot_id,
                "idempotency_key": f"{args.run_id}:marker:{index}"}
            marker = service.add_marker(**marker_arguments)
            marker_state = service.read()
            marker_replay = service.add_marker(**marker_arguments)
            if not marker_replay["replayed"] or service.read().snapshot_id != marker_state.snapshot_id:
                raise InvalidOperationError("Repeated marker request changed state.")
            report["trials"].append({"index": index + 1, "plan": plan.to_dict(), "append": insertion,
                "marker": marker, "append_replay_unchanged": True, "marker_replay_unchanged": True,
                "stale_snapshot_rejected": stale_rejected})
            checkpoint()
        final_state = service.read().to_dict()
        report["restoration"] = setup.select_timeline(original.timeline.id, idempotency_key=args.run_id + ":restore")
        selection_changed = False
        after_original = adapter.read_timeline()
        if after_original.snapshot_id != original.snapshot_id:
            raise InvalidOperationError("Original active timeline changed during the smoke test.")
        report.update(status="verified", original_unchanged=True, final_state=final_state)
        checkpoint()
        print(json.dumps({"status": "verified", "trials": len(report["trials"]), "original_unchanged": True,
                          "report": str(report_path), "test_timeline": target["name"]}, indent=2))
        return 0
    except ResolveBridgeError as exc:
        if report is not None:
            report.update(status="failed", error=exc.to_dict())
            if selection_changed:
                try:
                    report["restoration"] = setup.select_timeline(original.timeline.id, idempotency_key=args.run_id + ":restore")
                except ResolveBridgeError as restore_error:
                    report["restoration_error"] = restore_error.to_dict()
            report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"error": exc.to_dict()}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
