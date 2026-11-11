"""`resolve-bridge` command: doctor, init, paths, mcp.

    resolve-bridge doctor   Is Resolve Studio running, reachable and calibrated? Is the config usable?
    resolve-bridge init     Write a starter config (empty allowlist) to the default location.
    resolve-bridge paths    Print the config, journal, backup and report locations in use.
    resolve-bridge mcp      Start the stdio MCP server (same as resolve-bridge-mcp).

doctor only reads: it never launches Resolve, opens a project, selects a timeline or writes a project.
"""

import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

from . import __version__, calibration
from ._api import _call, _id, _text
from .config import EXAMPLE_CONFIG, ConfigError, ServerConfig, default_config_path
from .errors import NoProjectError, ResolveBridgeError

OK, WARN, FAIL = "ok", "warn", "fail"


def _resolve_process_running() -> bool | None:
    """Best effort; None when the platform gives no cheap answer."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            return "Resolve.exe" in out
        # macOS names the process "Resolve"; Linux uses "resolve".
        result = subprocess.run(["pgrep", "-i", "-x", "resolve"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def run_checks(config_path=None, connect=None, process_running=_resolve_process_running) -> list[dict]:
    """Each check is {name, status, detail}. Later checks are skipped when one they need has failed."""
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})
        return status

    bits = struct.calcsize("P") * 8
    version = ".".join(map(str, sys.version_info[:3]))
    add("python", OK if sys.version_info >= (3, 10) and bits == 64 else FAIL,
        f"Python {version}, {bits}-bit at {sys.executable}. Needs 3.10+ and 64-bit; the MCP server must run in this same interpreter.")

    config = None
    try:
        config = ServerConfig.resolve(config_path)
    except ConfigError as exc:
        add("config", FAIL, str(exc))
    else:
        if config.source is None:
            add("config", WARN, f"No config file at {default_config_path()}. Using safe defaults: empty allowlist, "
                                "so every project read and write is refused. Run `resolve-bridge init`.")
        else:
            writable = sum(1 for entry in config.allowed_projects.values() if entry["allow_writes"])
            add("config", OK, f"{config.source}: {len(config.allowed_projects)} allowed project(s), {writable} writable.")
        calibration.register(config.calibrated_builds)
        try:
            config.journal_path.parent.mkdir(parents=True, exist_ok=True)
            probe = config.journal_path.parent / ".doctor-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            add("journal", OK, f"{config.journal_path} (folder is writable). Backups: {config.effective_backup_dir()}")
        except OSError as exc:
            add("journal", FAIL, f"Cannot write in {config.journal_path.parent}: {exc}")

    running = process_running()
    if running is False:
        add("resolve_running", FAIL, "No DaVinci Resolve process found. Open Resolve Studio with a project, then rerun.")
    else:
        add("resolve_running", OK if running else WARN,
            "DaVinci Resolve process found." if running else "Could not check the process list; relying on the connection test.")

    adapter = None
    try:
        if connect is None:
            from .connection import connect
        adapter = connect()
    except ResolveBridgeError as exc:
        hint = ("" if running is False else " Resolve seems to be running, so check Preferences > System > General > "
                "External scripting using = Local, and that this is Resolve Studio (the free edition has no external scripting).")
        add("resolve_connect", FAIL, f"[{exc.code}] {exc}{hint}")
    if adapter is None:
        return checks
    add("resolve_connect", OK, f"Connected through {adapter._module_path} ({adapter._loading_method}).")

    try:
        product = _text(_call(adapter._resolve, "GetProductName"), "GetProductName")
        build = _text(_call(adapter._resolve, "GetVersionString"), "GetVersionString")
    except ResolveBridgeError as exc:
        add("resolve_edition", FAIL, f"[{exc.code}] {exc}")
        return checks
    add("resolve_edition", OK if "studio" in product.lower() else WARN,
        product if "studio" in product.lower() else f"{product}: not reported as Studio. External scripting is a Studio feature.")
    if calibration.is_calibrated(build):
        add("resolve_build", OK, f"{build} is a calibrated build. Writes are available.")
    else:
        add("resolve_build", WARN, f"{build} is not a calibrated build ({calibration.describe()}). The server is "
                                   "read-only on this build until you follow docs/RECALIBRATION.md.")

    try:
        project = adapter._project()
        project_id, name = _id(project), _text(_call(project, "GetName"), "Project.GetName")
    except NoProjectError:
        add("project", WARN, "No project is open in Resolve.")
    except ResolveBridgeError as exc:
        add("project", FAIL, f"[{exc.code}] {exc}")
    else:
        entry = config.allowed_projects.get(project_id) if config else None
        if entry is None:
            snippet = json.dumps({"id": project_id, "name": name, "allow_writes": False})
            add("project", WARN, f"Open project \"{name}\" is not in the allowlist. To allow reads, add to allowed_projects: {snippet}")
        else:
            add("project", OK, f"Open project \"{name}\" ({project_id}) is allowlisted, "
                               f"{'writes allowed' if entry['allow_writes'] else 'read-only'}.")
    return checks


def doctor(args) -> int:
    checks = run_checks(args.config)
    failed = any(check["status"] == FAIL for check in checks)
    if args.json:
        print(json.dumps({"version": __version__, "ok": not failed, "checks": checks}, indent=2, ensure_ascii=False))
    else:
        for check in checks:
            print(f"[{check['status'].upper():4}] {check['name']}: {check['detail']}")
        print("\nReady." if not failed else "\nNot ready: fix the FAIL lines above.")
    return 1 if failed else 0


def init(args) -> int:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    if path.exists() and not args.force:
        print(f"{path} already exists; not overwriting (use --force).", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}\nNext: open your project in Resolve and run `resolve-bridge doctor` for its id.")
    return 0


def paths(args) -> int:
    try:
        config = ServerConfig.resolve(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"config_file": str(config.source) if config.source else None,
                      "default_config_file": str(default_config_path()), "journal_path": str(config.journal_path),
                      "backup_dir": str(config.effective_backup_dir()), "reports_dir": str(config.reports_dir()),
                      "RESOLVE_SCRIPT_API": os.environ.get("RESOLVE_SCRIPT_API"),
                      "RESOLVE_SCRIPT_LIB": os.environ.get("RESOLVE_SCRIPT_LIB")}, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="resolve-bridge", description="DaVinci Resolve bridge utilities.")
    parser.add_argument("--version", action="version", version=f"resolve-bridge {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, function, summary in (("doctor", doctor, "Check Python, config, and the connection to Resolve Studio."),
                                    ("init", init, "Write a starter config with an empty allowlist."),
                                    ("paths", paths, "Print the locations in use."),
                                    ("mcp", None, "Start the stdio MCP server.")):
        command = commands.add_parser(name, help=summary, description=summary)
        command.add_argument("--config", default=None, help="Config JSON (default: $RESOLVE_BRIDGE_CONFIG or the user config folder).")
        command.set_defaults(function=function)
        if name == "doctor":
            command.add_argument("--json", action="store_true", help="Machine-readable output.")
        if name == "init":
            command.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    args = parser.parse_args(argv)
    if args.command == "mcp":
        from .mcp_server import main as serve
        return serve(["--config", args.config] if args.config else [])
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
