"""Version the normalized edit-relevant fields, not all of Resolve's hidden state."""

from dataclasses import asdict
import hashlib
import json

from .errors import StaleSnapshotError


def snapshot_id(state) -> str:
    payload = {"schema_version": state.schema_version, "project": asdict(state.project), "timeline": asdict(state.timeline)}
    timeline = payload["timeline"]
    timeline["tracks"] = sorted(timeline["tracks"], key=lambda track: (track["type"], track["index"]))
    for track in timeline["tracks"]:
        track["items"] = sorted(track["items"], key=lambda item: item["id"])
    timeline["markers"] = sorted(timeline["markers"], key=lambda marker: marker["offset_frame"])
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return "timeline-v2-" + hashlib.sha256(encoded).hexdigest()


def require_snapshot(state, expected: str) -> None:
    if not isinstance(expected, str) or expected != state.snapshot_id:
        raise StaleSnapshotError("Timeline state changed or snapshot is invalid. Read and plan again before executing.")
