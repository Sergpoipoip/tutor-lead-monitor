import json
import logging

import pytest

from tutor_lead_monitor.cli import main
from tutor_lead_monitor.logging import JsonFormatter


def test_logs_exclude_exception_arguments_and_arbitrary_fields() -> None:
    record = logging.LogRecord(
        "tutor_lead_monitor.test",
        logging.ERROR,
        __file__,
        1,
        "collection_failed",
        ("synthetic-sentinel",),
        None,
    )
    record.run_id = "run-1"
    record.token = "synthetic-sentinel"
    try:
        raise ValueError("synthetic-sentinel")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    output = JsonFormatter().format(record)
    assert "synthetic-sentinel" not in output
    assert json.loads(output)["run_id"] == "run-1"


def test_dependency_log_text_is_not_forwarded() -> None:
    record = logging.LogRecord("sqlalchemy.engine", logging.ERROR, "", 0, "secret-url", (), None)
    assert "secret-url" not in JsonFormatter().format(record)


def test_cli_config_and_fixture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/unused")
    assert main(["check-config"]) == 0
    assert main(["fixture"]) == 0
    output = capsys.readouterr()
    assert "fixture-v1-1" in output.out
    assert "Ищу репетитора" not in output.out


def test_cli_failure_redacts_inputs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "synthetic-sentinel")
    assert main(["check-config"]) == 1
    output = capsys.readouterr()
    assert "synthetic-sentinel" not in output.err
    assert "ValidationError" in output.err
