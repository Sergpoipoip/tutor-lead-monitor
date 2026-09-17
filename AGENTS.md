# Tutor Lead Monitor

Read these authoritative documents completely before making changes:

- [Product specification and milestones](docs/PROJECT_SPEC.md)
- [Architecture and domain contracts](docs/ARCHITECTURE.md)
- [Source policy and registry](docs/SOURCES.md)

Implement only the requested milestone. Use Python 3.12+, PostgreSQL,
SQLAlchemy 2, Alembic, and pytest. Keep collectors independent of processing
and delivery; preserve raw evidence and enforce database idempotency.
Use UTC internally. Never commit secrets or bypass source access controls.
Add meaningful tests for parsing, filtering, persistence, and deduplication
as those features are implemented. Run the checks documented in README.md.
