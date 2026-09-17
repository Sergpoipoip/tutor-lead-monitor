import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

MAINTENANCE = "tlm:maintenance"
DEDUPLICATION = "tlm:deduplication"


class AlreadyRunning(RuntimeError):
    """A competing invocation already owns this source."""


def lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def transaction_lock(session: Session, name: str, *, shared: bool = False) -> None:
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    session.execute(text(f"SELECT {function}(:key)"), {"key": lock_key(name)})


@contextmanager
def source_lock(engine: Engine, source_key: str) -> Iterator[Connection]:
    """Dedicated connection owns the session lock across page commits."""
    key = lock_key(f"tlm:source:{source_key}")
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        connection.commit()
        if not acquired:
            raise AlreadyRunning("Source collection is already running")
        try:
            yield connection
        finally:
            try:
                connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
            except Exception:
                # Never return a connection with an uncertain session lock to the pool.
                connection.invalidate()
                raise
