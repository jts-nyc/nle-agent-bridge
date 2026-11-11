#!/usr/bin/env python
"""Print Media Pool inventory or search candidates without changing Resolve."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge.connection import connect
from resolve_bridge.errors import ResolveBridgeError
from resolve_bridge.media import find_media


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query")
    parser.add_argument("--exact", action="store_true")
    args = parser.parse_args()
    try:
        inventory = connect().read_media()
        if args.query is None:
            result = inventory.to_dict()
        else:
            matches = find_media(inventory, args.query, exact=args.exact)
            result = {"query": args.query, "ambiguous": len(matches) > 1, "matches": [asdict(item) for item in matches]}
    except ResolveBridgeError as exc:
        print(json.dumps({"error": exc.to_dict()}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
