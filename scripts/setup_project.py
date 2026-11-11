#!/usr/bin/env python
"""Plan or create a scoped Media Pool workspace and an isolated test timeline."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge.config import ServerConfig
from resolve_bridge.connection import connect
from resolve_bridge.errors import ProjectScopeError, ResolveBridgeError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.project import ProjectSetup, ROOT_BIN, WORKSPACE_BINS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--test-name", default="AI_BRIDGE_TEST_BASELINE")
    parser.add_argument("--execute", action="store_true", help="Actually create bins and a test timeline; default is read-only planning.")
    args = parser.parse_args()
    try:
        adapter = connect()
        before = adapter.read_media()
        if before.project.id != args.project_id:
            raise ProjectScopeError("The active project does not match --project-id.")
        plan = {"project": before.project.name, "project_id": args.project_id, "root_bin": ROOT_BIN, "bins": WORKSPACE_BINS, "test_timeline": args.test_name, "source_file_changes": []}
        if not args.execute:
            result = {"plan": plan, "executed": False}
        else:
            journal = OperationJournal(ServerConfig.resolve().journal_path)
            setup = ProjectSetup(adapter, args.project_id, journal)
            workspace = setup.create_workspace(idempotency_key=f"setup:{args.project_id}:v1")
            timeline = setup.create_test_timeline(args.test_name, idempotency_key=f"timeline:{args.project_id}:{args.test_name}")
            result = {"plan": plan, "executed": True, "workspace": workspace, "timeline": timeline}
    except ResolveBridgeError as exc:
        print(json.dumps({"error": exc.to_dict()}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
