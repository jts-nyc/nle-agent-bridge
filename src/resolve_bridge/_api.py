"""Internal native-call and value-validation helpers for adapter modules only."""

import math
from typing import Any

from .errors import ResolveReadError, ResolveWriteError, UnexpectedAPIResponseError


def _call(obj: Any, method: str, *args: object) -> Any:
    try:
        function = getattr(obj, method, None)
        if not callable(function):
            raise UnexpectedAPIResponseError(f"Resolve does not expose the documented {method} method.")
        return function(*args)
    except UnexpectedAPIResponseError:
        raise
    except Exception as exc:
        raise ResolveReadError(
            f"Resolve failed during {method}; the connection or active context may have changed."
        ) from exc


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise UnexpectedAPIResponseError(f"{field} must return {'a string' if allow_empty else 'a nonempty string'}.")
    return value


def _write(obj: Any, method: str, *args: object) -> Any:
    try:
        return _call(obj, method, *args)
    except ResolveReadError as exc:
        raise ResolveWriteError(
            f"Resolve failed during {method}; the operation may already be partly applied. Inspect state before another request."
        ) from exc


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnexpectedAPIResponseError(f"{field} must return an integer frame/count.")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise UnexpectedAPIResponseError(f"{field} returned a fractional or nonfinite frame/count; refusing to round it.")
    return int(value)


def _id(obj: Any) -> str:
    return _text(_call(obj, "GetUniqueId"), "GetUniqueId")


def _list(value: Any, field: str) -> list | tuple:
    if not isinstance(value, (list, tuple)):
        raise UnexpectedAPIResponseError(f"{field} must return a list; API failure is not an empty list.")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise UnexpectedAPIResponseError(f"{field} must return a boolean.")
    return value
