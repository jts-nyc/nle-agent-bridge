"""Load Blackmagic's installed module and connect without starting or changing Resolve."""

import importlib
import os
from pathlib import Path
import sys
from types import ModuleType

from .errors import ResolveUnavailableError, ScriptingModuleUnavailableError
from .timeline import ResolveAdapter


def _default_api_root() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting")
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting"
    return Path("/opt/resolve/Developer/Scripting")


def _load_scripting_module() -> tuple[ModuleType, str]:
    """Use the installed wrapper; it resolves fusionscript and RESOLVE_SCRIPT_LIB.

    Blackmagic's wrapper replaces its own sys.modules entry with the native
    extension. Ordinary import machinery intentionally preserves that behavior.
    """
    api_override = os.environ.get("RESOLVE_SCRIPT_API")
    try:
        if not api_override:
            try:
                return importlib.import_module("DaVinciResolveScript"), "python_import_path"
            except ModuleNotFoundError as exc:
                if exc.name != "DaVinciResolveScript":
                    raise
        root = Path(api_override).expanduser() if api_override else _default_api_root()
        module_dir = root / "Modules"
        if not (module_dir / "DaVinciResolveScript.py").is_file():
            raise ScriptingModuleUnavailableError(
                f"DaVinciResolveScript.py was not found in {module_dir}. "
                "Set RESOLVE_SCRIPT_API to Resolve's Developer/Scripting directory."
            )
        sys.path.insert(0, str(module_dir))
        try:
            module = importlib.import_module("DaVinciResolveScript")
        finally:
            sys.path.pop(0)
        return module, "RESOLVE_SCRIPT_API" if api_override else "default_installation"
    except ScriptingModuleUnavailableError:
        raise
    except Exception as exc:
        raise ScriptingModuleUnavailableError(
            "Could not load Resolve's scripting module or native fusionscript library. "
            "Use a compatible 64-bit Python and check RESOLVE_SCRIPT_API and "
            f"RESOLVE_SCRIPT_LIB. Loader reported {type(exc).__name__}: {exc}"
        ) from exc


def connect() -> ResolveAdapter:
    """Return the normalized read adapter, never a live Resolve object."""
    module, method = _load_scripting_module()
    try:
        resolve = module.scriptapp("Resolve")
    except Exception as exc:
        raise ResolveUnavailableError(
            "Resolve's scripting service could not be reached. Ensure Resolve is running "
            "and external scripting is enabled for Local access."
        ) from exc
    if resolve is None:
        raise ResolveUnavailableError(
            "Resolve did not provide a scripting connection. Open Resolve with a project "
            "and set Preferences > System > General > External scripting using to Local. "
            "External scripting availability depends on the Resolve edition."
        )
    return ResolveAdapter(resolve, str(getattr(module, "__file__", "unknown")), method)
