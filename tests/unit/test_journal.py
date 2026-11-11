from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from resolve_bridge.errors import IdempotencyConflictError, OperationUncertainError
from resolve_bridge.journal import OperationJournal


def test_repeat_across_instances_executes_once(tmp_path):
    path = tmp_path / "journal.sqlite3"
    action = Mock(return_value={"verified": True})
    first = OperationJournal(path).execute("marker", {"project_id": "p"}, "key", action)
    repeat = OperationJournal(path).execute("marker", {"project_id": "p"}, "key", action)
    assert first["operation_id"] == repeat["operation_id"]
    assert repeat["replayed"] is True
    action.assert_called_once()
    assert OperationJournal(path).get(first["operation_id"])["status"] == "verified"


def test_key_reuse_with_different_scope_or_params_fails(tmp_path):
    journal = OperationJournal(tmp_path / "j.db")
    journal.execute("marker", {"project_id": "p"}, "key", lambda _: {})
    action = Mock()
    with pytest.raises(IdempotencyConflictError):
        journal.execute("marker", {"project_id": "q"}, "key", action)
    action.assert_not_called()


def test_failed_attempt_is_never_reexecuted(tmp_path):
    journal = OperationJournal(tmp_path / "j.db")
    action = Mock(side_effect=RuntimeError("API may have changed state"))
    with pytest.raises(RuntimeError):
        journal.execute("marker", {}, "key", action)
    with pytest.raises(OperationUncertainError):
        journal.execute("marker", {}, "key", action)
    action.assert_called_once()


def test_crash_leaves_running_intent_and_blocks_retry(tmp_path):
    journal = OperationJournal(tmp_path / "j.db")
    with pytest.raises(KeyboardInterrupt):
        journal.execute("marker", {}, "key", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(OperationUncertainError, match="running"):
        journal.execute("marker", {}, "key", Mock())


def test_concurrent_duplicate_runs_at_most_once(tmp_path):
    journal = OperationJournal(tmp_path / "j.db")
    action = Mock(return_value={})

    def attempt():
        try:
            return journal.execute("marker", {}, "key", action)
        except OperationUncertainError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: attempt(), range(8)))
    action.assert_called_once()


def test_different_keys_cannot_mutate_concurrently(tmp_path):
    path = tmp_path / "j.db"
    first, second = OperationJournal(path), OperationJournal(path)
    action = Mock()

    def outer(_):
        with pytest.raises(OperationUncertainError, match="in progress"):
            second.execute("marker", {}, "different-key", action)
        return {}

    first.execute("marker", {}, "first-key", outer)
    action.assert_not_called()
