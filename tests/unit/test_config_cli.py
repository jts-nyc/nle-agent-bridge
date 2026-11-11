"""Config locations, safe defaults, the build pin, and the doctor/init commands. No Resolve."""

import json
from unittest.mock import Mock

import pytest

from resolve_bridge import calibration, cli
from resolve_bridge.config import ConfigError, ServerConfig, default_config_path
from resolve_bridge.errors import ResolveUnavailableError
from resolve_bridge.mcp_server import BridgeTools, MCPServer

ENV = ("RESOLVE_BRIDGE_CONFIG", "RESOLVE_BRIDGE_MCP_CONFIG", "RESOLVE_BRIDGE_STATE_DIR", "RESOLVE_BRIDGE_JOURNAL",
       "RESOLVE_BRIDGE_BACKUP_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A clean per-user environment rooted in tmp_path."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def pin():
    before = set(calibration._builds)
    yield
    calibration._builds.clear()
    calibration._builds.update(before)


def call(server, name, **arguments):
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return response["result"]["isError"], response["result"]["structuredContent"]


def test_no_config_file_means_empty_allowlist_and_state_folder(home):
    config = ServerConfig.resolve()
    assert config.source is None and config.allowed_projects == {} and config.midi_roots == ()
    assert config.journal_path == home / "state" / "resolve-bridge" / "operations.sqlite3"
    assert config.effective_backup_dir() == home / "state" / "resolve-bridge" / "project_backups"
    assert config.write_timeline_prefix == "AI_BRIDGE_TEST_"
    assert default_config_path() == home / "cfg" / "resolve-bridge" / "config.json"


def test_env_overrides_and_explicit_backup_dir(home, monkeypatch):
    path = home / "elsewhere.json"
    path.write_text(json.dumps({"allowed_projects": [{"id": "p"}], "journal_path": "j/ops.sqlite3", "backup_dir": "drp"}), encoding="utf-8")
    monkeypatch.setenv("RESOLVE_BRIDGE_CONFIG", str(path))
    config = ServerConfig.resolve()
    assert config.source == path.resolve() and config.journal_path == (home / "j" / "ops.sqlite3").resolve()
    assert config.effective_backup_dir() == (home / "drp").resolve()
    monkeypatch.setenv("RESOLVE_BRIDGE_JOURNAL", str(home / "env.sqlite3"))
    monkeypatch.setenv("RESOLVE_BRIDGE_BACKUP_DIR", str(home / "envdrp"))
    config = ServerConfig.resolve()
    assert config.journal_path == (home / "env.sqlite3").resolve() and config.effective_backup_dir() == (home / "envdrp").resolve()
    monkeypatch.setenv("RESOLVE_BRIDGE_CONFIG", str(home / "missing.json"))
    with pytest.raises(ConfigError):  # a config named by the environment must exist
        ServerConfig.resolve()


def test_state_dir_override(home, monkeypatch):
    monkeypatch.setenv("RESOLVE_BRIDGE_STATE_DIR", str(home / "s"))
    assert ServerConfig.resolve().journal_path == (home / "s").resolve() / "operations.sqlite3"


def test_empty_allowlist_refuses_project_reads(home, scene):
    server = MCPServer(BridgeTools(ServerConfig.resolve(), connect=lambda: scene.adapter), log=lambda text: None)
    is_error, payload = call(server, "read_media", project_id="project-1")
    assert is_error and payload["error"]["code"] == "project_scope_mismatch"
    is_error, status = call(server, "resolve_status")
    assert not is_error and status["allowlist"] == [] and status["config_file"] is None
    assert status["project"]["allowlisted"] is False and status["active_timeline"] is None


def test_uncalibrated_build_is_read_only_until_registered(home, scene, pin):
    scene.resolve.GetVersionString.return_value = "99.0.0.1"
    config = ServerConfig.from_dict({"allowed_projects": [{"id": "project-1", "allow_writes": True}],
                                     "write_timeline_prefix": ""}, base=home)
    server = MCPServer(BridgeTools(config, connect=lambda: scene.adapter), log=lambda text: None)
    is_error, status = call(server, "resolve_status")
    assert status["calibrated_build"] is False and status["writes_available"] is False
    assert not call(server, "list_timelines", project_id="project-1")[0]  # reads still work
    scope = {"project_id": "project-1", "idempotency_key": "k"}
    for tool, arguments in (("select_timeline", {"timeline_id": "timeline-1"}),
                            ("ensure_video_track", {"timeline_id": "timeline-1", "index": 2}),
                            ("save_and_backup", {"label": "x"})):
        is_error, payload = call(server, tool, **scope, **arguments)
        assert is_error and payload["error"]["code"] == "uncalibrated_build", tool
    assert not (home / "state" / "resolve-bridge" / "operations.sqlite3").exists()  # refused before the journal
    calibration.register(("99.0.0.1",))
    assert call(server, "resolve_status")[1]["writes_available"] is True


def test_backup_lands_in_configured_backup_dir(home, scene):
    from resolve_bridge.journal import OperationJournal
    from resolve_bridge.project import ProjectSetup
    journal = OperationJournal(home / "j.sqlite3")
    assert ProjectSetup(scene.adapter, "project-1", journal).backup_dir == home / "project_backups"
    assert ProjectSetup(scene.adapter, "project-1", journal, backup_dir=home / "drp").backup_dir == home / "drp"


def test_doctor_passes_with_studio_and_reports_unlisted_project(home, scene, capsys):
    checks = {c["name"]: c for c in cli.run_checks(connect=lambda: scene.adapter, process_running=lambda: True)}
    assert checks["resolve_connect"]["status"] == "ok" and checks["resolve_edition"]["status"] == "ok"
    assert checks["resolve_build"]["status"] == "ok" and checks["config"]["status"] == "warn"
    assert checks["project"]["status"] == "warn" and '"id": "project-1"' in checks["project"]["detail"]
    assert not any(c["status"] == "fail" for c in checks.values())


def test_doctor_fails_when_resolve_is_not_running(home):
    connect = Mock(side_effect=ResolveUnavailableError("Resolve did not provide a scripting connection."))
    checks = {c["name"]: c for c in cli.run_checks(connect=connect, process_running=lambda: False)}
    assert checks["resolve_running"]["status"] == "fail" and checks["resolve_connect"]["status"] == "fail"
    assert "resolve_edition" not in checks
    running = {c["name"]: c for c in cli.run_checks(connect=connect, process_running=lambda: True)}
    assert "External scripting" in running["resolve_connect"]["detail"]


def test_doctor_warns_on_free_edition_and_uncalibrated_build(home, scene, pin):
    scene.resolve.GetProductName.return_value = "DaVinci Resolve"
    scene.resolve.GetVersionString.return_value = "22.0.0.1"
    checks = {c["name"]: c for c in cli.run_checks(connect=lambda: scene.adapter, process_running=lambda: None)}
    assert checks["resolve_edition"]["status"] == "warn" and checks["resolve_build"]["status"] == "warn"
    assert "RECALIBRATION" in checks["resolve_build"]["detail"] and checks["resolve_running"]["status"] == "warn"


def test_doctor_command_exit_codes_and_json(home, monkeypatch, capsys, scene):
    monkeypatch.setattr(cli, "_resolve_process_running", lambda: False)
    monkeypatch.setattr("resolve_bridge.connection.connect", Mock(side_effect=ResolveUnavailableError("closed")))
    monkeypatch.setattr(cli.run_checks, "__defaults__", (None, None, lambda: False))
    assert cli.main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and {c["name"] for c in report["checks"]} >= {"python", "config", "resolve_connect"}
    assert cli.main(["doctor", "--config", str(home / "nope.json")]) == 1
    assert "Cannot read MCP config" in capsys.readouterr().out


def test_init_writes_starter_config_once(home, capsys):
    assert cli.main(["init"]) == 0
    path = default_config_path()
    config = ServerConfig.resolve()
    assert config.source == path.resolve() and config.allowed_projects == {}
    assert cli.main(["init"]) == 1 and "already exists" in capsys.readouterr().err
    capsys.readouterr()
    assert cli.main(["paths"]) == 0
    assert json.loads(capsys.readouterr().out)["config_file"] == str(path.resolve())
