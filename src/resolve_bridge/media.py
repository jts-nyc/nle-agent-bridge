"""Read-only Media Pool traversal and pure search over normalized inventory."""

import unicodedata

from ._api import _call, _id, _list, _text
from .errors import AmbiguousMediaError, InvalidOperationError, MediaNotFoundError, UnexpectedAPIResponseError
from .models import BinState, MediaInventory, MediaItemState, ProjectState
from .timecode import parse_frame_rate


def _property(properties: dict, key: str) -> str | None:
    value = properties.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise UnexpectedAPIResponseError(f"GetClipProperty returned an unexpected value for {key}.")
    return str(value)


def _read_inventory(project) -> MediaInventory:
    project_state = ProjectState(_id(project), _text(_call(project, "GetName"), "Project.GetName"))
    pool = _call(project, "GetMediaPool")
    root = _call(pool, "GetRootFolder")
    bins, items = [], []
    seen_bins, seen_items = set(), set()
    pending = [(root, None, ())]
    while pending:
        folder, parent_id, parent_path = pending.pop()
        folder_id = _id(folder)
        if folder_id in seen_bins:
            raise UnexpectedAPIResponseError("Media Pool contains a repeated folder ID or a cycle.")
        seen_bins.add(folder_id)
        name = _text(_call(folder, "GetName"), "Folder.GetName")
        path = parent_path + (name,)
        bins.append(BinState(folder_id, name, parent_id, path))
        for native in _list(_call(folder, "GetClipList"), "GetClipList"):
            media_id = _id(native)
            if media_id in seen_items:
                raise UnexpectedAPIResponseError("Media Pool returned a repeated media ID during traversal.")
            seen_items.add(media_id)
            props = _call(native, "GetClipProperty")
            if not isinstance(props, dict):
                raise UnexpectedAPIResponseError("GetClipProperty must return a dictionary.")
            warnings = []
            raw_frames = _property(props, "Frames")
            duration = None
            if raw_frames and raw_frames.isascii() and raw_frames.isdecimal():
                duration = int(raw_frames)
            elif raw_frames:
                warnings.append(f"Unrecognized source frame count: {raw_frames}")
            raw_rate = _property(props, "FPS")
            rate = None
            if raw_rate:
                try:
                    rate = parse_frame_rate(raw_rate)
                except UnexpectedAPIResponseError:
                    warnings.append(f"Unrecognized source frame rate: {raw_rate}")
            source_path = _property(props, "File Path")
            file_name = _property(props, "File Name")
            if file_name is None and source_path:
                file_name = source_path.replace("\\", "/").rsplit("/", 1)[-1]
            raw_start = _property(props, "Start")
            source_start = int(raw_start) if raw_start is not None and raw_start.isascii() and raw_start.isdecimal() else None
            items.append(MediaItemState(
                media_id, _text(_call(native, "GetName"), "MediaPoolItem.GetName"),
                folder_id, path, file_name, source_path, _property(props, "Type"),
                duration, rate, tuple(warnings), source_start, _property(props, "Online Status"),
            ))
        children = _list(_call(folder, "GetSubFolderList"), "GetSubFolderList")
        pending.extend((child, folder_id, path) for child in reversed(children))
    return MediaInventory(1, project_state, tuple(bins), tuple(items))


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def find_media(inventory: MediaInventory, query: str, *, exact: bool = False) -> tuple[MediaItemState, ...]:
    """Match filename or clip name, returning every candidate without choosing."""
    if not isinstance(query, str) or not query.strip() or type(exact) is not bool:
        raise InvalidOperationError("Search requires a nonempty query and a boolean exact flag.")
    normalized = _normalize(query)
    matches = []
    for item in inventory.items:
        # Timeline entries are not source footage. Unknown types remain visible.
        if item.media_type and item.media_type.casefold() == "timeline":
            continue
        names = {_normalize(item.name)}
        if item.file_name:
            names.add(_normalize(item.file_name))
        if any(normalized == name if exact else normalized in name for name in names):
            matches.append(item)
    return tuple(matches)


def require_unique_media(inventory: MediaInventory, query: str, *, exact: bool = False) -> MediaItemState:
    matches = find_media(inventory, query, exact=exact)
    if not matches:
        raise MediaNotFoundError(f"No media matches {query!r}.")
    if len(matches) != 1:
        details = ", ".join(f"{item.id} in {' / '.join(item.bin_path)}" for item in matches)
        raise AmbiguousMediaError(f"{len(matches)} media items match {query!r}; choose a stable media ID: {details}")
    return matches[0]
