#!/usr/bin/env python
"""Start the Resolve bridge MCP server on stdio. Claude Code launches this; see docs/MCP.md."""

from pathlib import Path
import sys

# Support running directly from a clean source checkout, like the other scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resolve_bridge.mcp_server import main

if __name__ == "__main__":
    raise SystemExit(main())
