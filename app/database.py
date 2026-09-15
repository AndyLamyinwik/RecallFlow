"""SQLAlchemy engine, session lifecycle, and numbered schema initialization."""

from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import Column, Engine, Integer, MetaData, String, Table, create_engine, event, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings
from app.db_types import UTCDateTime


class Base(DeclarativeBase):
    pass


schema_metadata = MetaData()
schema_version = Table(
    "schema_version",
    schema_metadata,
    Column("version", Integer, primary_key=True),
    Column("name", String(120), nullable=False),
    Column("applied_at", UTCDateTime(), nullable=False),
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Connection], None]


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite:///:memory:":
        return
    database_path = Path(database_url.removeprefix(prefix))
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)


def build_engine(database_url: str) -> Engine:
    _ensure_sqlite_parent(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


settings = get_settings()
engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def _migration_001_minimum_data_model(connection: Connection) -> None:
    # Importing registers model tables with Base.metadata before creation.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=connection)


def _migration_002_capture_v1_fields(connection: Connection) -> None:
    """Add Milestone 2 columns to databases already initialized at v1."""

    existing = {column["name"] for column in inspect(connection).get_columns("saved_items")}
    additions = {
        "source_text_hash": "VARCHAR(64)",
        "source_char_count": "INTEGER",
        "source_truncated": "BOOLEAN NOT NULL DEFAULT 0",
        "extraction_error_type": "VARCHAR(40)",
        "extraction_version": "VARCHAR(80) NOT NULL DEFAULT 'extraction/v1'",
        "effort_rule_version": "VARCHAR(80) NOT NULL DEFAULT 'effort_rule/v1'",
        "schedule_timezone": "VARCHAR(64)",
    }
    for column_name, definition in additions.items():
        if column_name not in existing:
            connection.exec_driver_sql(
                f"ALTER TABLE saved_items ADD COLUMN {column_name} {definition}"
            )


def _migration_003_unique_pilot_access_code(connection: Connection) -> None:
    """Add an opaque unique lookup key for atomic invitation-code claiming."""

    existing = {column["name"] for column in inspect(connection).get_columns("users")}
    if "access_code_fingerprint" not in existing:
        connection.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN access_code_fingerprint VARCHAR(64)"
        )

    indexes = {index["name"] for index in inspect(connection).get_indexes("users")}
    if "ux_user_access_code_fingerprint" not in indexes:
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX ux_user_access_code_fingerprint "
            "ON users (access_code_fingerprint)"
        )


def _migration_004_resurfacing_actions(connection: Connection) -> None:
    """Add the two timestamps required by the staged Open transition."""

    item_columns = {
        column["name"] for column in inspect(connection).get_columns("saved_items")
    }
    if "last_source_opened_at" not in item_columns:
        connection.exec_driver_sql(
            "ALTER TABLE saved_items ADD COLUMN last_source_opened_at DATETIME"
        )

    inspector = inspect(connection)
    if inspector.has_table("resurfacing_events"):
        event_columns = {
            column["name"]
            for column in inspector.get_columns("resurfacing_events")
        }
        if "open_in_flight_until" not in event_columns:
            connection.exec_driver_sql(
                "ALTER TABLE resurfacing_events "
                "ADD COLUMN open_in_flight_until DATETIME"
            )


MIGRATIONS = (
    Migration(1, "minimum_data_model", _migration_001_minimum_data_model),
    Migration(2, "capture_v1_fields", _migration_002_capture_v1_fields),
    Migration(3, "unique_pilot_access_code", _migration_003_unique_pilot_access_code),
    Migration(4, "resurfacing_actions", _migration_004_resurfacing_actions),
)


def initialize_database(target_engine: Engine | None = None) -> None:
    """Apply every unapplied migration exactly once, in version order."""

    selected_engine = target_engine or engine
    with selected_engine.begin() as connection:
        schema_metadata.create_all(bind=connection, tables=[schema_version], checkfirst=True)
        applied = set(connection.execute(select(schema_version.c.version)).scalars())
        known_versions = {migration.version for migration in MIGRATIONS}
        unknown_versions = applied - known_versions
        if unknown_versions:
            raise RuntimeError(
                f"Database schema is newer than this application: {sorted(unknown_versions)}"
            )
        if applied and applied != set(range(1, max(applied) + 1)):
            raise RuntimeError(f"Database schema has a version gap: {sorted(applied)}")
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            migration.apply(connection)
            connection.execute(
                schema_version.insert().values(
                    version=migration.version,
                    name=migration.name,
                    applied_at=datetime.now(timezone.utc),
                )
            )
