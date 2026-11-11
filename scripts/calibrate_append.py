#!/usr/bin/env python
"""Investigate source endpoint conventions in a new disposable test timeline.

This is a development probe, not an editing/controller API. Native parameter
ranges are fixed below; observed durations are evidence, not assumed correctness.
"""

import argparse
from dataclasses import asdict
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge._api import _call, _id, _write
from resolve_bridge.config import ServerConfig
from resolve_bridge.connection import connect
from resolve_bridge.errors import InvalidOperationError, ResolveBridgeError, VerificationError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.operations import EditingService
from resolve_bridge.project import ProjectSetup

CASES = ((0, 23), (0, 24), (90, 209), (90, 210), (100, 199), (100, 200), (900, 909), (900, 910))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--run-id", default="20260930",
                        help="Letters, digits, _ or -. Use a new one for every recalibration (e.g. the build number); "
                             "it names the test timeline, the journal keys and the report.")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.run_id) is None:
        parser.error("--run-id requires 1-40 letters, digits, underscores, or hyphens")
    run = args.run_id
    if not args.execute:
        print(json.dumps({"native_source_ranges": CASES, "executed": False}))
        return
    config = ServerConfig.resolve()
    report_path = config.reports_dir() / ("source-range-calibration.json" if run == "20260930" else f"source-range-calibration-{run}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    adapter = connect()
    original = adapter.read_timeline()
    journal = OperationJournal(config.journal_path)
    setup = ProjectSetup(adapter, args.project_id, journal)
    created = setup.create_test_timeline(f"AI_BRIDGE_TEST_{run}_CALIBRATION", idempotency_key=f"calibration-{run}-create")
    if created["replayed"]:
        raise InvalidOperationError("Calibration already attempted; inspect its report rather than repeating it.")
    timeline_id = created["result"]["timeline_id"]
    setup.select_timeline(timeline_id, idempotency_key=f"calibration-{run}-select")
    service = EditingService(adapter, args.project_id, timeline_id, journal)
    observations = []
    try:
        media = service._media(args.media_id)
        for index, (source_in, native_out) in enumerate(CASES):
            if media.duration_frames is None or native_out > media.duration_frames:
                continue

            def action(operation_id):
                before = service.read()
                project, _ = service._guard()
                pool, native = service._native_media(project, args.media_id)
                record = before.timeline.start_frame + index * 300
                service._guard()
                if _id(_call(project, "GetCurrentTimeline")) != timeline_id:
                    raise VerificationError("Active calibration timeline changed.")
                _write(pool, "AppendToTimeline", [{"mediaPoolItem": native, "startFrame": source_in,
                    "endFrame": native_out, "mediaType": 1, "trackIndex": 1, "recordFrame": record}])
                after = service.read()
                before_items = {item.id: item for track in before.timeline.tracks for item in track.items}
                after_items = {item.id: item for track in after.timeline.tracks for item in track.items}
                new_items = [item for key, item in after_items.items() if key not in before_items]
                if len(new_items) != 1 or any(after_items.get(key) != item for key, item in before_items.items()):
                    raise VerificationError("Calibration did not add exactly one item with earlier items unchanged.")
                item = new_items[0]
                if (item.media_id, item.start_frame, item.track_type, item.track_index) != (args.media_id, record, "video", 1):
                    raise VerificationError("Calibration inserted an unexpected item or location.")
                return {"native_source_in": source_in, "native_source_out": native_out, "observed_item": asdict(item),
                        "snapshot_before": before.snapshot_id, "snapshot_after": after.snapshot_id}

            observations.append(journal.execute("calibrate_append_range", {"project_id": args.project_id,
                "timeline_id": timeline_id, "media_id": args.media_id, "source_in": source_in, "native_out": native_out},
                f"calibration-{run}-case-{index}", action))
            report_path.write_text(json.dumps(observations, indent=2) + "\n")
    finally:
        setup.select_timeline(original.timeline.id, idempotency_key=f"calibration-{run}-restore")
    if adapter.read_timeline().snapshot_id != original.snapshot_id:
        raise VerificationError("Original timeline changed during calibration.")
    print(json.dumps({"resolve_version": original.connection.resolve_version, "report": str(report_path),
                      "observations": [entry["result"] for entry in observations]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ResolveBridgeError as exc:
        print(json.dumps({"error": exc.to_dict()}), file=sys.stderr)
        raise SystemExit(1)
