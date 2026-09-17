import os
import subprocess
import sys


def test_ci_cannot_silently_skip_integration_tests() -> None:
    environment = {key: value for key, value in os.environ.items() if key != "TEST_DATABASE_URL"}
    environment["REQUIRE_INTEGRATION_TESTS"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "tests/integration/test_database.py::test_migration_round_trip_and_metadata",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode != 0
    assert "TEST_DATABASE_URL is required in CI" in result.stdout
