from uuid import UUID

import pytest

from tutor_lead_monitor.application.failed import validate_selection
from tutor_lead_monitor.cli import main


@pytest.mark.parametrize("limit", [-1, 0, 1001])
def test_failed_batch_bounds(limit: int) -> None:
    with pytest.raises(ValueError):
        validate_selection(None, limit)


def test_failed_selection_requires_one_explicit_selector() -> None:
    with pytest.raises(ValueError):
        validate_selection(None, None)
    with pytest.raises(ValueError):
        validate_selection(UUID(int=1), 1)
    validate_selection(UUID(int=1), None)
    validate_selection(None, 1)
    validate_selection(None, 1000)


@pytest.mark.parametrize(
    "args",
    [
        ["retry-failed"],
        ["retry-failed", "--limit", "1001"],
        ["failed", "--limit", "0"],
        ["failed", "--limit", "1001"],
        ["retry-failed", "--record-id", str(UUID(int=1)), "--limit", "1"],
        ["retry-failed", "--limit", "1", "--dry-run"],
        ["retry-failed", "--limit", "1", "--apply"],
    ],
)
def test_invalid_failed_commands_do_not_open_database(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = False

    def forbidden_connection(*args: object, **kwargs: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("Must validate before database access")

    monkeypatch.setattr("tutor_lead_monitor.cli.create_db_engine", forbidden_connection)
    assert main(args) == 1
    assert not opened
