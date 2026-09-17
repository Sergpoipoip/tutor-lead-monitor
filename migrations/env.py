from alembic import context
from sqlalchemy import Connection

from tutor_lead_monitor.config import Settings
from tutor_lead_monitor.db import models  # noqa: F401
from tutor_lead_monitor.db.base import Base
from tutor_lead_monitor.db.session import create_db_engine

target_metadata = Base.metadata


def run_connected(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    # Offline SQL generation does not need credentials.
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied_connection = context.config.attributes.get("connection")
    if supplied_connection is not None:
        run_connected(supplied_connection)
    else:
        engine = create_db_engine(Settings())
        try:
            with engine.connect() as connection:
                run_connected(connection)
        finally:
            engine.dispose()
