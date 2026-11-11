"""Narrow, verified project setup. Never touches source files on disk."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import zipfile

from ._api import _call, _id, _integer, _list, _text, _write
from .errors import InvalidOperationError, ProjectScopeError, VerificationError

ROOT_BIN = "AI Bridge"
WORKSPACE_BINS = ("01 Footage", "02 Audio", "03 Graphics", "04 VFX", "05 Timelines", "90 Tests")


def inventory_fingerprint(inventory):
    payload = inventory.to_dict()
    payload["bins"] = sorted(payload["bins"], key=lambda item: item["id"])
    payload["items"] = sorted(payload["items"], key=lambda item: item["id"])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


class ProjectSetup:
    """Adapter-side project setup, bound to one explicitly authorized project ID."""

    def __init__(self, adapter, project_id: str, journal, backup_dir=None):
        if not isinstance(project_id, str) or not project_id:
            raise InvalidOperationError("Project setup requires a stable project ID.")
        self._adapter = adapter
        self.project_id = project_id
        self.journal = journal
        # Default keeps .drp exports beside the journal, as before backup_dir existed.
        self.backup_dir = Path(backup_dir) if backup_dir is not None else journal.path.parent / "project_backups"

    def _guard(self):
        project = self._adapter._project()
        if _id(project) != self.project_id:
            raise ProjectScopeError("The active project is outside the authorized scope. No write was attempted.")
        return project

    def select_timeline(self, timeline_id: str, *, idempotency_key: str) -> dict:
        self._guard()

        def action(operation_id):
            project = self._guard()
            timeline = self._adapter._timeline_by_id(project, timeline_id)
            previous = _call(project, "GetCurrentTimeline")
            previous_id = _id(previous) if previous is not None else None
            if previous_id != timeline_id:
                self._guard()
                _write(project, "SetCurrentTimeline", timeline)
            self._guard()
            if _id(_call(project, "GetCurrentTimeline")) != timeline_id:
                raise VerificationError("Requested timeline selection was not observed on reread.")
            return {"previous_timeline_id": previous_id, "selected_timeline_id": timeline_id}

        return self.journal.execute("select_timeline", {"project_id": self.project_id, "timeline_id": timeline_id}, idempotency_key, action)

    def _ensure_bin(self, pool, parent, name):
        matches = [folder for folder in _list(_call(parent, "GetSubFolderList"), "GetSubFolderList") if _call(folder, "GetName") == name]
        if len(matches) > 1:
            raise InvalidOperationError(f"Multiple bins named {name!r}; cannot choose a destination.")
        if matches:
            return matches[0]
        self._guard()
        created = _write(pool, "AddSubFolder", parent, name)
        created_id = _id(created)
        after = _list(_call(parent, "GetSubFolderList"), "GetSubFolderList")
        if not any(_id(folder) == created_id and _call(folder, "GetName") == name for folder in after):
            raise VerificationError(f"Resolve did not expose the newly created bin {name!r} on reread.")
        return created

    def create_workspace(self, *, idempotency_key: str) -> dict:
        self._guard()
        parameters = {"project_id": self.project_id, "root": ROOT_BIN, "bins": WORKSPACE_BINS}

        def action(operation_id):
            before = self._adapter.read_media()
            project = self._guard()
            pool = _call(project, "GetMediaPool")
            root = self._ensure_bin(pool, _call(pool, "GetRootFolder"), ROOT_BIN)
            ids = {ROOT_BIN: _id(root)}
            for name in WORKSPACE_BINS:
                ids[name] = _id(self._ensure_bin(pool, root, name))
            after = self._adapter.read_media()
            if not set(ids.values()) <= {folder.id for folder in after.bins}:
                raise VerificationError("Workspace bins were missing on final reread.")
            previous_media = {(item.id, item.bin_id, item.source_path) for item in before.items}
            if not previous_media <= {(item.id, item.bin_id, item.source_path) for item in after.items}:
                raise VerificationError("Existing media changed during workspace setup.")
            return {"project_id": self.project_id, "bins": ids, "media_unchanged": True}

        return self.journal.execute("create_workspace", parameters, idempotency_key, action)

    def create_test_timeline(self, name: str, *, idempotency_key: str) -> dict:
        self._guard()
        if not isinstance(name, str) or not name.startswith("AI_BRIDGE_TEST_") or len(name) > 120 or any(c in name for c in "/\\\n\r"):
            raise InvalidOperationError("Test timeline name must start with AI_BRIDGE_TEST_ and contain no path separators.")
        parameters = {"project_id": self.project_id, "name": name}

        def action(operation_id):
            project = self._guard()
            count = _integer(_call(project, "GetTimelineCount"), "GetTimelineCount")
            timelines = [_call(project, "GetTimelineByIndex", i) for i in range(1, count + 1)]
            if any(_call(timeline, "GetName") == name for timeline in timelines):
                raise InvalidOperationError("A timeline with this name already exists; choose a new test name.")
            before_ids = {_id(timeline) for timeline in timelines}
            original = _call(project, "GetCurrentTimeline")
            pool = _call(project, "GetMediaPool")
            root = self._ensure_bin(pool, _call(pool, "GetRootFolder"), ROOT_BIN)
            tests = self._ensure_bin(pool, root, "90 Tests")
            self._guard()
            created = _write(pool, "CreateEmptyTimeline", name)
            created_id = _id(created)
            self._guard()
            _write(pool, "MoveClips", [_call(created, "GetMediaPoolItem")], tests)
            in_tests = _list(_call(tests, "GetClipList"), "GetClipList")
            media_id = _id(_call(created, "GetMediaPoolItem"))
            if media_id not in {_id(item) for item in in_tests}:
                raise VerificationError("Created timeline was not found in the test bin.")
            # Restore prior selection after successful setup; do not hide a failed
            # operation behind an automatic recovery mutation.
            if original is not None:
                self._guard()
                _write(project, "SetCurrentTimeline", original)
                if _id(_call(project, "GetCurrentTimeline")) != _id(original):
                    raise VerificationError("Original timeline selection was not restored.")
            after_count = _integer(_call(project, "GetTimelineCount"), "GetTimelineCount")
            after_ids = {_id(_call(project, "GetTimelineByIndex", i)) for i in range(1, after_count + 1)}
            if after_ids != before_ids | {created_id} or created_id in before_ids:
                raise VerificationError("Timeline list changed unexpectedly during setup.")
            self._guard()
            return {"project_id": self.project_id, "timeline_id": created_id, "name": name, "bin_id": _id(tests), "original_timeline_id": _id(original) if original is not None else None}

        return self.journal.execute("create_test_timeline", parameters, idempotency_key, action)

    def plan_organization(self) -> dict:
        """Propose moving root-bin entries only. Filesystem paths never change."""
        self._guard()
        inventory = self._adapter.read_media()
        if inventory.project.id != self.project_id:
            raise ProjectScopeError("Project changed during organization planning.")
        roots = [folder for folder in inventory.bins if folder.parent_id is None]
        if len(roots) != 1:
            raise InvalidOperationError("Media Pool must have exactly one root bin.")
        root = roots[0]
        workspaces = [folder for folder in inventory.bins if folder.parent_id == root.id and folder.name == ROOT_BIN]
        if len(workspaces) != 1:
            raise InvalidOperationError("Create exactly one AI Bridge workspace before organizing media.")
        children = [folder for folder in inventory.bins if folder.parent_id == workspaces[0].id]
        mapping = {"Video + Audio": "01 Footage", "Video": "01 Footage", "Audio": "02 Audio", "Still": "03 Graphics", "Timeline": "05 Timelines"}
        moves, unresolved = [], []
        for item in inventory.items:
            if item.bin_id != root.id:
                continue
            destination_name = mapping.get(item.media_type)
            destinations = [folder for folder in children if folder.name == destination_name]
            if destination_name is None or len(destinations) != 1:
                unresolved.append({"media_id": item.id, "name": item.name, "reason": "Unknown media type or ambiguous/missing destination bin"})
                continue
            destination = destinations[0]
            moves.append({"media_id": item.id, "name": item.name, "from_bin_id": item.bin_id,
                          "to_bin_id": destination.id, "to_bin_path": destination.path})
        return {"project_id": self.project_id, "inventory_fingerprint": inventory_fingerprint(inventory),
                "moves": moves, "unresolved": unresolved, "source_file_changes": []}

    def execute_organization(self, plan: dict, *, idempotency_key: str) -> dict:
        self._guard()
        if not isinstance(plan, dict) or plan.get("project_id") != self.project_id:
            raise ProjectScopeError("Organization plan belongs to a different project.")

        def action(operation_id):
            # Recompute the exact allowed plan. Callers cannot smuggle arbitrary
            # targets or source items through an edited JSON plan.
            current_plan = self.plan_organization()
            if json.dumps(plan, sort_keys=True) != json.dumps(current_plan, sort_keys=True):
                raise InvalidOperationError("Media inventory or organization plan changed; create a fresh plan.")
            before = self._adapter.read_media()
            project = self._guard()
            pool = _call(project, "GetMediaPool")
            folders, media = {}, {}
            pending = [_call(pool, "GetRootFolder")]
            while pending:
                folder = pending.pop()
                folder_id = _id(folder)
                if folder_id in folders:
                    raise InvalidOperationError("Repeated folder while organizing media.")
                folders[folder_id] = folder
                media.update({_id(item): item for item in _list(_call(folder, "GetClipList"), "GetClipList")})
                pending.extend(_list(_call(folder, "GetSubFolderList"), "GetSubFolderList"))
            expected_bins = {item.id: item.bin_id for item in before.items}
            for move in plan["moves"]:
                self._guard()
                source_folder = folders[move["from_bin_id"]]
                if move["media_id"] not in {_id(item) for item in _list(_call(source_folder, "GetClipList"), "GetClipList")}:
                    raise InvalidOperationError("Media moved since planning; stopping before this write.")
                _write(pool, "MoveClips", [media[move["media_id"]]], folders[move["to_bin_id"]])
                after_move = self._adapter.read_media()
                moved = next((item for item in after_move.items if item.id == move["media_id"]), None)
                if moved is None or moved.bin_id != move["to_bin_id"]:
                    raise VerificationError("Media Pool move was not observed on reread.")
                expected_bins[move["media_id"]] = move["to_bin_id"]
            after = self._adapter.read_media()
            if {item.id: item.bin_id for item in after.items} != expected_bins:
                raise VerificationError("Media Pool inventory changed unexpectedly during organization.")
            before_properties = {item.id: {key: value for key, value in asdict(item).items() if key not in ("bin_id", "bin_path")} for item in before.items}
            after_properties = {item.id: {key: value for key, value in asdict(item).items() if key not in ("bin_id", "bin_path")} for item in after.items}
            if before_properties != after_properties:
                raise VerificationError("Media metadata or source paths changed during bin organization.")
            self._guard()
            return {"moves": plan["moves"], "unresolved": plan["unresolved"], "source_paths_unchanged": True,
                    "inventory_before": inventory_fingerprint(before), "inventory_after": inventory_fingerprint(after)}

        return self.journal.execute("organize_media_pool", plan, idempotency_key, action)

    def save_and_backup(self, label: str, *, idempotency_key: str) -> dict:
        """Save the scoped project and export a DRP into the backup folder."""
        self._guard()
        if not isinstance(label, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label) is None:
            raise InvalidOperationError("Backup label requires 1–64 letters, digits, underscores, or hyphens.")
        # Hash the project ID for a filesystem-safe, unambiguous name.
        identifier = hashlib.sha256(self.project_id.encode()).hexdigest()[:12]
        output = self.backup_dir / f"{identifier}-{label}.drp"

        def action(operation_id):
            before = self._adapter.read_timeline()
            if output.exists():
                raise InvalidOperationError("Backup file already exists; choose a new label rather than overwrite it.")
            project = self._guard()
            manager = _call(self._adapter._resolve, "GetProjectManager")
            if _write(manager, "SaveProject") is not True:
                raise VerificationError("Resolve did not acknowledge saving the project.")
            output.parent.mkdir(parents=True, exist_ok=True)
            self._guard()
            if _write(manager, "ExportProject", _text(_call(project, "GetName"), "Project.GetName"), str(output.resolve()), False) is not True:
                raise VerificationError("Resolve did not acknowledge exporting the project backup.")
            if not output.is_file() or output.stat().st_size == 0:
                raise VerificationError("Exported project backup is absent or empty.")
            try:
                with zipfile.ZipFile(output) as archive:
                    if not archive.namelist() or archive.testzip() is not None:
                        raise VerificationError("Project backup archive failed integrity validation.")
            except zipfile.BadZipFile as exc:
                raise VerificationError("Exported DRP is not a readable ZIP archive.") from exc
            after = self._adapter.read_timeline()
            if after.snapshot_id != before.snapshot_id:
                raise VerificationError("Timeline changed while saving/exporting the backup.")
            self._guard()
            return {"project_id": self.project_id, "save_acknowledged": True, "backup_path": str(output.resolve()),
                    "backup_bytes": output.stat().st_size, "archive_integrity_verified": True,
                    "restore_tested": False, "timeline_unchanged": True}

        return self.journal.execute("save_and_backup", {"project_id": self.project_id, "label": label}, idempotency_key, action)
