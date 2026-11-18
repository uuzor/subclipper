"""Database engine and session management.

The engine is created lazily on first use so that configuration (including a
test-specific database URL) is applied before any connection is opened.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings
from .logging_setup import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    connect_args = (
        {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    )
    return create_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def reset_engine() -> None:
    """Drop cached engine/sessionmaker. Used by tests when config changes."""
    get_sessionmaker.cache_clear()
    engine = get_engine()
    engine.dispose()
    get_engine.cache_clear()


def init_db() -> None:
    # Import models so they register with the metadata before create_all.
    from . import models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _add_missing_columns(engine)
    _backfill_workspaces(engine)


def _backfill_workspaces(engine: Engine) -> None:
    """Give pre-tenancy rows a workspace.

    Tenancy was added after the content tables existed, so rows written before it
    have a NULL ``workspace_id``. Left alone they would be invisible to every
    scoped query — not deleted, but unreachable, which is worse because it looks
    like data loss. They are adopted into a single deterministic "legacy"
    workspace instead, so an operator can see them and move them deliberately.

    Runs only when there is something to adopt, so a fresh database does no work.
    """
    from sqlalchemy import text

    from . import auth_service

    scoped_tables = ("projects", "publish_targets", "channel_subscriptions")
    with engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).all()
        }
        if not tables:
            # Not SQLite. Use the inspector, which works on both backends.
            from sqlalchemy import inspect

            tables = set(inspect(engine).get_table_names())

        pending: dict[str, int] = {}
        for table in scoped_tables:
            if table not in tables:
                continue
            count = connection.execute(
                text(f'SELECT COUNT(*) FROM "{table}" WHERE workspace_id IS NULL')
            ).scalar()
            if count:
                pending[table] = int(count)

    if not pending:
        return

    session = get_sessionmaker()()
    try:
        workspace = auth_service.ensure_legacy_workspace(session)
        for table in pending:
            session.execute(
                text(f'UPDATE "{table}" SET workspace_id = :wid WHERE workspace_id IS NULL'),
                {"wid": workspace.id},
            )
        session.commit()
        logger.warning(
            "db.backfilled_workspaces",
            workspace_id=workspace.id,
            adopted=pending,
            detail="rows predating tenancy were adopted into the legacy workspace",
        )
    finally:
        session.close()


def _add_missing_columns(engine: Engine) -> None:
    """Add columns that exist on the models but not yet in the database.

    There is no migration framework, and ``create_all`` never alters an existing
    table. Without this, adding a column breaks every existing installation with
    "no such column". Only additive, nullable/defaulted columns are handled,
    which is all the schema has needed so far.
    """
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable and column.default is None and column.server_default is None:
                logger.warning(
                    "db.skipped_required_column",
                    table=table.name,
                    column=column.name,
                    reason="cannot add a NOT NULL column without a default",
                )
                continue
            column_type = column.type.compile(engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'
            with engine.begin() as connection:
                connection.execute(text(ddl))
            logger.info("db.added_column", table=table.name, column=column.name)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
