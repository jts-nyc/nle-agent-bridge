"""Where the bridge keeps its config and state, and what the config may contain.

Nothing here is specific to one machine. Every location has a default under the
user's own config/state directories and can be overridden:

    RESOLVE_BRIDGE_CONFIG       config file (also --config)
    RESOLVE_BRIDGE_STATE_DIR    folder for the journal, backups and reports
    RESOLVE_BRIDGE_JOURNAL      journal file, overriding the config's journal_path
    RESOLVE_BRIDGE_BACKUP_DIR   .drp backup folder, overriding the config's backup_dir

With no config file at all the server still starts, with an empty allowlist: it can
report status and convert timecode, and refuses every project read and write.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

APP_DIR = "resolve-bridge"
DEFAULT_WRITE_PREFIX = "AI_BRIDGE_TEST_"
CONFIG_KEYS = {"allowed_projects", "journal_path", "backup_dir", "write_timeline_prefix", "midi_roots",
               "calibrated_builds", "_comment"}

EXAMPLE_CONFIG = {
    "_comment": "Run `resolve-bridge doctor` with your project open to get its id. Relative paths resolve "
                "against this file's folder. Omit journal_path and backup_dir to use the state folder.",
    "allowed_projects": [],
    "write_timeline_prefix": DEFAULT_WRITE_PREFIX,
    "midi_roots": [],
    "calibrated_builds": [],
}


class ConfigError(Exception):
    pass


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    if sys.platform == "win32":
        base = _env_path("APPDATA") or Path.home() / "AppData" / "Roaming"
    else:
        base = _env_path("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / APP_DIR


def state_dir() -> Path:
    override = _env_path("RESOLVE_BRIDGE_STATE_DIR")
    if override:
        return override.resolve()
    if sys.platform == "win32":
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        base = _env_path("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return base / APP_DIR


def default_config_path() -> Path:
    # RESOLVE_BRIDGE_MCP_CONFIG is the name earlier versions used.
    return (_env_path("RESOLVE_BRIDGE_CONFIG") or _env_path("RESOLVE_BRIDGE_MCP_CONFIG")
            or config_dir() / "config.json")


def default_journal_path() -> Path:
    return state_dir() / "operations.sqlite3"


@dataclass(frozen=True)
class ServerConfig:
    allowed_projects: dict  # project ID -> {"name": str, "allow_writes": bool}
    journal_path: Path
    write_timeline_prefix: str
    midi_roots: tuple = ()  # folders MIDI files may be read from; empty refuses every MIDI path
    backup_dir: Path | None = None  # None: project_backups/ beside the journal
    calibrated_builds: tuple = ()  # builds the user calibrated themselves; see docs/RECALIBRATION.md
    source: Path | None = None  # the file this came from, or None for built-in defaults

    @classmethod
    def load(cls, path: str | Path) -> "ServerConfig":
        path = Path(path).expanduser().resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read MCP config {path}: {exc}") from exc
        return cls.from_dict(raw, base=path.parent, source=path)

    @classmethod
    def resolve(cls, explicit: str | Path | None = None) -> "ServerConfig":
        """An explicit path must exist. The default location may be absent: safe defaults."""
        if explicit is not None:
            return cls.load(explicit)
        path = default_config_path()
        named_by_env = _env_path("RESOLVE_BRIDGE_CONFIG") or _env_path("RESOLVE_BRIDGE_MCP_CONFIG")
        if path.is_file() or named_by_env:
            return cls.load(path)
        return cls.from_dict({}, base=path.parent)

    @classmethod
    def from_dict(cls, raw: dict, *, base: Path, source: Path | None = None) -> "ServerConfig":
        if not isinstance(raw, dict) or set(raw) - CONFIG_KEYS:
            raise ConfigError("Config keys are allowed_projects, journal_path, backup_dir, "
                              "write_timeline_prefix, midi_roots, calibrated_builds.")

        def located(value, name):
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{name} must be a nonempty path string.")
            path = Path(value).expanduser()
            return (path if path.is_absolute() else base / path).resolve()

        projects = raw.get("allowed_projects", [])
        if not isinstance(projects, list):
            raise ConfigError("allowed_projects must be a list of {id, name, allow_writes}.")
        allowed = {}
        for entry in projects:
            if (not isinstance(entry, dict) or set(entry) - {"id", "name", "allow_writes"}
                    or not isinstance(entry.get("id"), str) or not entry["id"]
                    or not isinstance(entry.get("name", ""), str)
                    or type(entry.get("allow_writes", False)) is not bool):
                raise ConfigError("Each allowed project needs a string id, optional name, and boolean allow_writes.")
            if entry["id"] in allowed:
                raise ConfigError(f"Project {entry['id']} is listed twice.")
            allowed[entry["id"]] = {"name": entry.get("name", ""), "allow_writes": entry.get("allow_writes", False)}

        journal_override = _env_path("RESOLVE_BRIDGE_JOURNAL")
        if journal_override:
            journal_path = journal_override.resolve()
        elif "journal_path" in raw:
            journal_path = located(raw["journal_path"], "journal_path")
        else:
            journal_path = default_journal_path()

        backup_override = _env_path("RESOLVE_BRIDGE_BACKUP_DIR")
        if backup_override:
            backup_dir = backup_override.resolve()
        elif "backup_dir" in raw:
            backup_dir = located(raw["backup_dir"], "backup_dir")
        else:
            backup_dir = None

        prefix = raw.get("write_timeline_prefix", DEFAULT_WRITE_PREFIX)
        if not isinstance(prefix, str):
            raise ConfigError("write_timeline_prefix must be a string; use \"\" to allow any timeline.")
        roots = raw.get("midi_roots", [])
        if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
            raise ConfigError("midi_roots must be a list of folder paths.")
        builds = raw.get("calibrated_builds", [])
        if not isinstance(builds, list) or not all(isinstance(b, str) and b for b in builds):
            raise ConfigError("calibrated_builds must be a list of Resolve version strings.")
        return cls(allowed, journal_path, prefix, tuple(located(root, "midi_roots entry") for root in roots),
                   backup_dir, tuple(builds), source)

    def reports_dir(self) -> Path:
        return self.journal_path.parent / "reports"

    def effective_backup_dir(self) -> Path:
        return self.backup_dir or self.journal_path.parent / "project_backups"
