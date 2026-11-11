#!/usr/bin/env python
"""Read a scoped clip's Fusion compositions/graph without selecting a timeline."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge.connection import connect
from resolve_bridge.errors import ResolveBridgeError
from resolve_bridge.fusion import FusionAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--timeline-id", required=True)
    parser.add_argument("--clip-id", required=True)
    parser.add_argument("--comp-name")
    args = parser.parse_args()
    try:
        fusion = FusionAdapter(connect(), args.project_id, args.timeline_id)
        result = fusion.read_graph(args.clip_id, args.comp_name) if args.comp_name else {"compositions": fusion.list_compositions(args.clip_id)}
        print(json.dumps(result, indent=2))
        return 0
    except ResolveBridgeError as exc:
        print(json.dumps({"error": exc.to_dict()}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
