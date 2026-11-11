import json
from pathlib import Path
import runpy
from unittest.mock import Mock

import pytest

from resolve_bridge import connection
from resolve_bridge.errors import NoActiveTimelineError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inspect_resolve.py"


def test_cli_json_stdout(monkeypatch, capsys, scene):
    monkeypatch.setattr(connection, "connect", lambda: scene.adapter)
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main() == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["timeline"]["name"] == "TEST_TIMELINE"


def test_cli_typed_error_stderr(monkeypatch, capsys):
    monkeypatch.setattr(connection, "connect", Mock(side_effect=NoActiveTimelineError("Select a test timeline.")))
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["code"] == "no_active_timeline"
    assert "Traceback" not in output.err


def test_fusion_report_unicode_roundtrip(tmp_path):
    save = runpy.run_path(str(SCRIPT.parent / "fusion_demo.py"))["save_report"]
    path = tmp_path / "report.json"
    report = {"text": "Text+ • animé", "status": "verified"}
    save(path, report)
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_failed_fusion_report_serialization_preserves_previous_report(tmp_path):
    save = runpy.run_path(str(SCRIPT.parent / "fusion_demo.py"))["save_report"]
    path = tmp_path / "report.json"
    save(path, {"status": "running"})
    with pytest.raises(TypeError):
        save(path, {"native_object": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "running"}
