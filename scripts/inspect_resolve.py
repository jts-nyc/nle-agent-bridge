#!/usr/bin/env python
"""Print normalized JSON for the active timeline without changing Resolve."""

import json
from pathlib import Path
import sys

# Support the requested command directly from a clean source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge.connection import connect
from resolve_bridge.errors import ResolveBridgeError


def main() -> int:
    try:
        state = connect().read_timeline()
    except ResolveBridgeError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
