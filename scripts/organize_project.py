#!/usr/bin/env python
"""Preview or execute verified Media Pool bin organization; never move files."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge.config import ServerConfig
from resolve_bridge.connection import connect
from resolve_bridge.errors import ProjectScopeError, ResolveBridgeError, VerificationError
from resolve_bridge.journal import OperationJournal
from resolve_bridge.project import ProjectSetup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    if args.execute and not args.idempotency_key:
        parser.error("--execute requires --idempotency-key")
    try:
        adapter = connect()
        before = adapter.read_timeline()
        if before.project.id != args.project_id:
            raise ProjectScopeError("Active project is outside the requested scope.")
        # The journal stores the exact plan used on execution for audit/replay.
        setup = ProjectSetup(adapter, args.project_id, OperationJournal(ServerConfig.resolve().journal_path))
        plan = setup.plan_organization()
        if args.execute:
            result = setup.execute_organization(plan, idempotency_key=args.idempotency_key)
            after = adapter.read_timeline()
            if after.snapshot_id != before.snapshot_id:
                raise VerificationError("Timeline content changed while organizing Media Pool bins.")
            output = {"executed": True, "plan": plan, "operation": result, "timeline_unchanged": True}
        else:
            output = {"executed": False, "plan": plan}
    except ResolveBridgeError as exc:
        print(json.dumps({"error": exc.to_dict()}), file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
