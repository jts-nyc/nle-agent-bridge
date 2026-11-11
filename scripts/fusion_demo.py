#!/usr/bin/env python
"""Create a silent, isolated Text+ demo; default is a read-only preview.

Writes use the same project-scoped journal as editing. Leaves the demonstration
for inspection and restores the original active timeline, including on failure.
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
from resolve_bridge.errors import InvalidOperationError, ResolveBridgeError, StaleSnapshotError, VerificationError
from resolve_bridge.fusion import FusionService
from resolve_bridge.journal import OperationJournal
from resolve_bridge.operations import EditingService
from resolve_bridge.project import ProjectSetup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--text", default="FUSION / VFX LAB\nText+ • animated opacity")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,48}", args.run_id) is None:
        parser.error("Run ID requires 1–48 letters, digits, underscores or hyphens.")
    config = ServerConfig.resolve()
    calibration.register(config.calibrated_builds)
    path = config.reports_dir() / f"fusion-{args.run_id}.json"
    report, setup, original, fusion, view_context = None, None, None, None, None
    changed = False
    failure = None
    try:
        adapter = connect()
        original = adapter.read_timeline()
        if original.project.id != args.project_id:
            raise InvalidOperationError("The open project does not match --project-id.")
        name = "AI_BRIDGE_TEST_" + args.run_id + "_VFX"
        if not args.execute:
            print(json.dumps({"executed": False, "project": original.project.name, "new_timeline": name,
                "source_range": [90, 330], "text": args.text, "fade_frames": 12, "audio": False,
                "effects": "Create one 240-frame video clip, Text+ and Merge, with four opacity keyframes; restore the original timeline."}, indent=2))
            return 0
        if path.exists():
            raise InvalidOperationError("A report already exists for this run ID. Inspect it before another attempt.")
        journal = OperationJournal(config.journal_path)
        setup = ProjectSetup(adapter, args.project_id, journal)
        key = "fusion-demo:" + args.run_id
        report = {"status": "running", "project_id": args.project_id, "original_snapshot": original.snapshot_id,
                  "verification_scope": "graph_and_timeline", "visual_verification": "not_performed"}
        path.parent.mkdir(parents=True, exist_ok=True)
        def checkpoint():
            save_report(path, report)
        checkpoint()
        report["setup"] = setup.create_test_timeline(name, idempotency_key=key + ":create")
        tid = report["setup"]["result"]["timeline_id"]
        # Set before selection so finally can restore even if read-back fails.
        changed = True
        report["selection"] = setup.select_timeline(tid, idempotency_key=key + ":select")
        checkpoint()
        edits = EditingService(adapter, args.project_id, tid, journal)
        state = edits.read()
        plan = edits.plan_append_clip(media_id=args.media_id, source_in_frame=90, source_out_frame=330,
            track_type="video", track_index=1, record_frame=state.timeline.start_frame, expected_snapshot_id=state.snapshot_id)
        report["append"] = edits.execute_append_clip(plan, idempotency_key=key + ":append")
        state = edits.read()
        clips = [i for t in state.timeline.tracks if t.type == "video" for i in t.items]
        if len(clips) != 1:
            raise VerificationError("Demo timeline should contain exactly one video clip.")
        clip_id = clips[0].id
        report.update(timeline_id=tid, clip_id=clip_id)
        checkpoint()
        fusion = FusionService(adapter, args.project_id, tid, journal)
        report["composition"] = fusion.create_composition(clip_id, expected_snapshot_id=state.snapshot_id, idempotency_key=key + ":comp")
        _, target_timeline = adapter._context()
        from resolve_bridge._api import _call
        # Save before preparation so failures can still restore page/playhead.
        view_context = {"project_id": args.project_id, "timeline_id": tid,
            "page": _call(adapter._resolve, "GetCurrentPage"), "timecode": _call(target_timeline, "GetCurrentTimecode")}
        comp_name = report["composition"]["result"]["graph"]["comp_name"]
        report["preparation"] = fusion.prepare_composition(clip_id, comp_name, idempotency_key=key + ":prepare")
        graph = fusion.read_graph(clip_id, comp_name)
        checkpoint()
        effect_plan = fusion.plan_lower_third(clip_id=clip_id, comp_name=graph["comp_name"], text=args.text,
            expected_snapshot_id=state.snapshot_id, expected_graph_id=graph["graph_id"])
        report["plan"] = effect_plan.to_dict()
        checkpoint()
        report["effect"] = fusion.execute_lower_third(effect_plan, idempotency_key=key + ":effect")
        after = fusion.read_graph(clip_id, graph["comp_name"])
        replay = fusion.execute_lower_third(effect_plan, idempotency_key=key + ":effect")
        if not replay["replayed"] or fusion.read_graph(clip_id, graph["comp_name"])["graph_id"] != after["graph_id"]:
            raise VerificationError("Replaying the effect changed its graph.")
        report["replay_unchanged"] = True
        try:
            fusion.plan_lower_third(clip_id=clip_id, comp_name=graph["comp_name"], text=args.text,
                expected_snapshot_id=state.snapshot_id, expected_graph_id=graph["graph_id"])
        except StaleSnapshotError:
            report["stale_graph_rejected"] = True
        else:
            raise VerificationError("The stale Fusion graph was accepted.")
        report["status"] = "verified"
    except Exception as exc:
        failure = exc.to_dict() if isinstance(exc, ResolveBridgeError) else {"code": "unexpected_failure", "message": f"{type(exc).__name__}: {exc}"}
        if report is not None:
            report.update(status="failed", error=failure)
    finally:
        if changed and setup is not None:
            if view_context is not None:
                try:
                    report["view_restoration"] = fusion.restore_view(view_context, idempotency_key=key + ":restore-view")
                except Exception as exc:
                    failure = {"code": "view_restoration_failed", "message": str(exc)}
                    report.update(status="failed", view_restoration_error=failure)
            try:
                report["restoration"] = setup.select_timeline(original.timeline.id, idempotency_key=key + ":restore")
                if adapter.read_timeline().snapshot_id != original.snapshot_id:
                    raise VerificationError("The original timeline changed during the VFX demo.")
                report["original_unchanged"] = True
            except Exception as exc:
                failure = {"code": "restoration_failed", "message": str(exc)}
                report.update(status="failed", restoration_error=failure)
        if report is not None:
            save_report(path, report)
    if failure:
        print(json.dumps({"error": failure, "report": str(path) if report else None}), file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "timeline_id": report["timeline_id"], "clip_id": report["clip_id"],
                      "verification_scope": report["verification_scope"], "visual_verification": report["visual_verification"],
                      "original_unchanged": report["original_unchanged"], "report": str(path)}, indent=2))
    return 0


def save_report(path, report):
    # fusionscript can change the process locale. Always specify UTF-8 and keep
    # the previous report intact if serialization or writing fails.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
