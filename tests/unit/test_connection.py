import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resolve_bridge import connection
from resolve_bridge.errors import ResolveUnavailableError, ScriptingModuleUnavailableError


@pytest.fixture(autouse=True)
def clear_override(monkeypatch):
    monkeypatch.delenv("RESOLVE_SCRIPT_API", raising=False)


def test_module_already_on_python_path(monkeypatch):
    module = SimpleNamespace()
    monkeypatch.setattr(connection.importlib, "import_module", lambda name: module)
    assert connection._load_scripting_module() == (module, "python_import_path")


def test_default_installation_fallback(monkeypatch, tmp_path):
    modules = tmp_path / "Modules"
    modules.mkdir()
    (modules / "DaVinciResolveScript.py").touch()
    monkeypatch.setattr(connection, "_default_api_root", lambda: tmp_path)
    module = SimpleNamespace()
    calls = []

    def import_module(name):
        calls.append(name)
        if len(calls) == 1:
            raise ModuleNotFoundError(name, name=name)
        assert sys.path[0] == str(modules)
        return module

    before = list(sys.path)
    monkeypatch.setattr(connection.importlib, "import_module", import_module)
    assert connection._load_scripting_module() == (module, "default_installation")
    assert sys.path == before


def test_explicit_root_and_path_restoration_on_failure(monkeypatch, tmp_path):
    modules = tmp_path / "Modules"
    modules.mkdir()
    (modules / "DaVinciResolveScript.py").touch()
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path))
    monkeypatch.setattr(connection.importlib, "import_module", Mock(side_effect=ImportError("native library incompatible")))
    before = list(sys.path)
    with pytest.raises(ScriptingModuleUnavailableError, match="native library incompatible"):
        connection._load_scripting_module()
    assert sys.path == before


def test_missing_override_module(monkeypatch, tmp_path):
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path))
    with pytest.raises(ScriptingModuleUnavailableError, match="not found"):
        connection._load_scripting_module()


def test_dependency_import_failure_is_not_hidden_by_fallback(monkeypatch):
    importer = Mock(side_effect=ModuleNotFoundError("missing dependency", name="fusionscript"))
    monkeypatch.setattr(connection.importlib, "import_module", importer)
    with pytest.raises(ScriptingModuleUnavailableError, match="missing dependency"):
        connection._load_scripting_module()
    assert importer.call_count == 1


@pytest.mark.parametrize("response", [None, RuntimeError("disconnected")])
def test_resolve_unavailable(monkeypatch, response):
    scriptapp = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
    module = SimpleNamespace(scriptapp=scriptapp)
    monkeypatch.setattr(connection, "_load_scripting_module", lambda: (module, "test"))
    with pytest.raises(ResolveUnavailableError):
        connection.connect()
    scriptapp.assert_called_once_with("Resolve")


def test_connection_returns_adapter_only(monkeypatch, scene):
    module = SimpleNamespace(scriptapp=Mock(return_value=scene.resolve), __file__="/fake/fusionscript.so")
    monkeypatch.setattr(connection, "_load_scripting_module", lambda: (module, "test"))
    state = connection.connect().read_timeline()
    assert state.connection.resolve_version == "21.0.0.48"
