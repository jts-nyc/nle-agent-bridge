"""Pure field-level comparison of two normalized reads of the same timeline.

This explains what a snapshot mismatch consists of. It covers exactly the fields
the snapshot covers, so it inherits the same limits (no grades, effects, Fusion).
"""

from dataclasses import asdict

from .errors import InvalidOperationError

_TIMELINE_FIELDS = ("name", "start_frame", "end_frame", "start_timecode")
_TRACK_FIELDS = ("name", "locked", "enabled")


def _changed_fields(old: dict, new: dict, fields) -> dict:
    return {field: [old[field], new[field]] for field in fields if old[field] != new[field]}


def _keyed(entries, key):
    result = {}
    for entry in entries:
        result[key(entry)] = entry
    return result


def _compare(old: dict, new: dict, fields=None) -> dict:
    added = [new[key] for key in new if key not in old]
    removed = [old[key] for key in old if key not in new]
    changed = []
    for key in old:
        if key in new and old[key] != new[key]:
            names = fields or sorted(set(old[key]) | set(new[key]))
            changed.append({"key": key, "fields": _changed_fields(old[key], new[key], names)})
    return {"added": added, "removed": removed, "changed": changed}


def diff_states(before, after) -> dict:
    """Return added/removed/changed tracks, clips and markers between two reads."""
    if (before.project.id, before.timeline.id) != (after.project.id, after.timeline.id):
        raise InvalidOperationError("Snapshots belong to different projects or timelines; there is nothing to compare.")
    old, new = asdict(before.timeline), asdict(after.timeline)
    timeline = _changed_fields(old, new, _TIMELINE_FIELDS)
    if old["frame_rate"] != new["frame_rate"]:
        timeline["frame_rate"] = [old["frame_rate"]["raw"], new["frame_rate"]["raw"]]
    strip = lambda track: {k: v for k, v in track.items() if k != "items"}
    tracks = _compare(_keyed((strip(t) for t in old["tracks"]), lambda t: f"{t['type']}:{t['index']}"),
                      _keyed((strip(t) for t in new["tracks"]), lambda t: f"{t['type']}:{t['index']}"), _TRACK_FIELDS)
    items = _compare(_keyed((i for t in old["tracks"] for i in t["items"]), lambda i: i["id"]),
                     _keyed((i for t in new["tracks"] for i in t["items"]), lambda i: i["id"]))
    markers = _compare(_keyed(old["markers"], lambda m: m["offset_frame"]),
                       _keyed(new["markers"], lambda m: m["offset_frame"]))
    identical = before.snapshot_id == after.snapshot_id
    return {"identical": identical, "before_snapshot_id": before.snapshot_id, "after_snapshot_id": after.snapshot_id,
            "timeline": timeline, "tracks": tracks, "clips": items, "markers": markers}
