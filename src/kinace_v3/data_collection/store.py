from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..config import database_path, validate_database_path
from .models import (
    BaseRecord,
    DataCollectionModelError,
    RevisionStatus,
    canonical_json_bytes,
    normalize_dataset,
)
from .revisions import (
    REVISION_SCHEMA_VERSION,
    Revision,
    RevisionError,
    make_revision,
    normalize_status,
)


SCHEMA_VERSION = 2
INGESTION_RUN_STATUSES = ("running", "completed", "failed")
DATABASE_TIMEOUT_SECONDS = 60.0

DATASET_CURRENT_VIEWS = {
    "trade_calendar": "current_trade_calendar",
    "stock_metadata": "current_stock_metadata",
    "daily_bars": "current_daily_bars",
    "daily_basic_metrics": "current_daily_basic_metrics",
    "adjustment_factors": "current_adjustment_factors",
    "name_change_intervals": "current_name_change_intervals",
    "sector_membership": "current_sector_membership",
    "security_state": "current_security_state",
    "forward_labels": "current_forward_labels",
}


REVISION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS data_revisions (
    revision_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    dataset TEXT NOT NULL CHECK (
        dataset IN (
            'trade_calendar',
            'stock_metadata',
            'daily_bars',
            'daily_basic_metrics',
            'adjustment_factors',
            'name_change_intervals',
            'sector_membership',
            'security_state',
            'forward_labels'
        )
    ),
    natural_key_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'observed',
            'backfilled_reliable',
            'provisional',
            'invalid',
            'superseded'
        )
    ),
    record_json TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    supersedes_revision_id TEXT REFERENCES data_revisions(revision_id)
)
"""


REVISION_COLUMNS = (
    "revision_sequence",
    "revision_id",
    "schema_version",
    "dataset",
    "natural_key_json",
    "status",
    "record_json",
    "source",
    "observed_at_utc",
    "recorded_at_utc",
    "ingestion_run_id",
    "supersedes_revision_id",
)


class RevisionStoreError(ValueError):
    """Raised when revision-store input or lineage is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RevisionStoreError(f"{field} must be a non-empty string")
    return value


def _json_text(value: Mapping[str, Any], field: str) -> str:
    try:
        return canonical_json_bytes(dict(value)).decode("utf-8")
    except DataCollectionModelError as exc:
        raise RevisionStoreError(f"{field} is not canonical JSON: {exc}") from exc


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RevisionStoreError(f"stored JSON is invalid: {exc}") from exc


def _connect(target: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(target), timeout=DATABASE_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(DATABASE_TIMEOUT_SECONDS * 1000)}")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _revision_table_sql(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='data_revisions'"
    ).fetchone()
    return str(row[0] or "") if row is not None else ""


def _needs_revision_schema_migration(connection: sqlite3.Connection) -> bool:
    sql = _revision_table_sql(connection)
    if not sql:
        return False
    return "'security_state'" not in sql or "'forward_labels'" not in sql


def _migrate_revision_schema(connection: sqlite3.Connection) -> None:
    """Upgrade the dataset CHECK constraint without losing revision history."""
    if not _needs_revision_schema_migration(connection):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in (
            "DROP VIEW IF EXISTS current_forward_labels",
            "DROP VIEW IF EXISTS current_security_state",
            "DROP VIEW IF EXISTS current_sector_membership",
            "DROP VIEW IF EXISTS current_name_change_intervals",
            "DROP VIEW IF EXISTS current_adjustment_factors",
            "DROP VIEW IF EXISTS current_daily_basic_metrics",
            "DROP VIEW IF EXISTS current_daily_bars",
            "DROP VIEW IF EXISTS current_stock_metadata",
            "DROP VIEW IF EXISTS current_trade_calendar",
            "DROP VIEW IF EXISTS current_data_revisions",
            "DROP VIEW IF EXISTS current_revisions",
            "DROP VIEW IF EXISTS revision_history",
            "DROP TRIGGER IF EXISTS data_revisions_immutable_update",
            "DROP TRIGGER IF EXISTS data_revisions_immutable_delete",
            "ALTER TABLE data_revisions RENAME TO data_revisions_v1",
        ):
            connection.execute(statement)
        connection.execute(REVISION_TABLE_SQL)
        columns = ", ".join(REVISION_COLUMNS)
        connection.execute(
            f"INSERT INTO data_revisions ({columns}) SELECT {columns} FROM data_revisions_v1"
        )
        connection.execute("DROP TABLE data_revisions_v1")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def _initialize_schema(connection: sqlite3.Connection) -> None:
    _migrate_revision_schema(connection)
    connection.execute(REVISION_TABLE_SQL)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS data_collection_schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'completed', 'failed')
            ),
            metadata_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_data_revisions_natural_key
            ON data_revisions(dataset, natural_key_json, revision_sequence DESC);
        CREATE INDEX IF NOT EXISTS idx_data_revisions_status
            ON data_revisions(dataset, status);
        CREATE INDEX IF NOT EXISTS idx_data_revisions_supersedes
            ON data_revisions(supersedes_revision_id);

        CREATE TRIGGER IF NOT EXISTS data_revisions_immutable_update
        BEFORE UPDATE ON data_revisions
        BEGIN
            SELECT RAISE(ABORT, 'data revisions are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS data_revisions_immutable_delete
        BEFORE DELETE ON data_revisions
        BEGIN
            SELECT RAISE(ABORT, 'data revisions cannot be deleted');
        END;

        CREATE VIEW IF NOT EXISTS revision_history AS
        SELECT
            r.*,
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM data_revisions child
                    WHERE child.supersedes_revision_id = r.revision_id
                ) THEN 'superseded'
                ELSE r.status
            END AS effective_status,
            (
                SELECT child.revision_id
                FROM data_revisions child
                WHERE child.supersedes_revision_id = r.revision_id
                ORDER BY child.revision_sequence
                LIMIT 1
            ) AS superseded_by_revision_id,
            CASE
                WHEN r.status IN ('observed', 'backfilled_reliable') THEN 1
                ELSE 0
            END AS gate_eligible,
            CASE
                WHEN r.status IN ('observed', 'backfilled_reliable') THEN 1
                ELSE 0
            END AS training_eligible
        FROM data_revisions r;

        CREATE VIEW IF NOT EXISTS current_revisions AS
        WITH non_superseded AS (
            SELECT r.*
            FROM data_revisions r
            WHERE r.status <> 'superseded'
              AND NOT EXISTS (
                  SELECT 1
                  FROM data_revisions child
                  WHERE child.supersedes_revision_id = r.revision_id
              )
        )
        SELECT
            n.*,
            n.status AS effective_status,
            CAST(NULL AS TEXT) AS superseded_by_revision_id,
            CASE
                WHEN n.status IN ('observed', 'backfilled_reliable') THEN 1
                ELSE 0
            END AS gate_eligible,
            CASE
                WHEN n.status IN ('observed', 'backfilled_reliable') THEN 1
                ELSE 0
            END AS training_eligible
        FROM non_superseded n
        WHERE NOT EXISTS (
            SELECT 1
            FROM non_superseded newer
            WHERE newer.dataset = n.dataset
              AND newer.natural_key_json = n.natural_key_json
              AND newer.revision_sequence > n.revision_sequence
        );

        CREATE VIEW IF NOT EXISTS current_data_revisions AS
        SELECT * FROM current_revisions;

        CREATE VIEW IF NOT EXISTS current_trade_calendar AS
        SELECT * FROM current_revisions WHERE dataset = 'trade_calendar';

        CREATE VIEW IF NOT EXISTS current_stock_metadata AS
        SELECT * FROM current_revisions WHERE dataset = 'stock_metadata';

        CREATE VIEW IF NOT EXISTS current_daily_bars AS
        SELECT * FROM current_revisions WHERE dataset = 'daily_bars';

        CREATE VIEW IF NOT EXISTS current_daily_basic_metrics AS
        SELECT * FROM current_revisions WHERE dataset = 'daily_basic_metrics';

        CREATE VIEW IF NOT EXISTS current_adjustment_factors AS
        SELECT * FROM current_revisions WHERE dataset = 'adjustment_factors';

        CREATE VIEW IF NOT EXISTS current_name_change_intervals AS
        SELECT * FROM current_revisions WHERE dataset = 'name_change_intervals';

        CREATE VIEW IF NOT EXISTS current_sector_membership AS
        SELECT * FROM current_revisions WHERE dataset = 'sector_membership';

        CREATE VIEW IF NOT EXISTS current_security_state AS
        SELECT * FROM current_revisions WHERE dataset = 'security_state';

        CREATE VIEW IF NOT EXISTS current_forward_labels AS
        SELECT * FROM current_revisions WHERE dataset = 'forward_labels';
        """
    )
    connection.execute(
        """
        INSERT INTO data_collection_schema_metadata (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )


def initialize_revision_store(database: str | Path | None = None) -> Path:
    """Create the append-only data-collection schema and return the database path."""
    target = database_path() if database is None else validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _connect(target) as connection:
        _initialize_schema(connection)
    return target


initialize_store = initialize_revision_store


def _create_ingestion_run(
    connection: sqlite3.Connection,
    *,
    source: str,
    metadata: Mapping[str, Any] | None = None,
    started_at_utc: str | None = None,
    run_id: str | None = None,
) -> str:
    resolved_run_id = (
        str(uuid.uuid4()) if run_id is None or run_id == "" else _required_text(run_id, "run_id")
    )
    resolved_metadata = dict(metadata or {})
    connection.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, source, started_at_utc, completed_at_utc, status, metadata_json
        ) VALUES (?, ?, ?, NULL, 'running', ?)
        """,
        (
            resolved_run_id,
            _required_text(source, "source"),
            started_at_utc or _utc_now(),
            _json_text(resolved_metadata, "metadata"),
        ),
    )
    return resolved_run_id


def _ensure_ingestion_run(
    connection: sqlite3.Connection,
    *,
    ingestion_run_id: str | None,
    source: str,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[str, bool]:
    if ingestion_run_id is None or ingestion_run_id == "":
        return (
            _create_ingestion_run(
                connection, source=source, metadata=metadata
            ),
            True,
        )
    resolved = _required_text(ingestion_run_id, "ingestion_run_id")
    row = connection.execute(
        "SELECT 1 FROM ingestion_runs WHERE run_id=?", (resolved,)
    ).fetchone()
    if row is None:
        raise RevisionStoreError(f"ingestion_run_id is not recorded: {resolved}")
    return resolved, False


def _finish_ingestion_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str = "completed",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if status not in INGESTION_RUN_STATUSES:
        raise RevisionStoreError(
            f"ingestion run status must be one of {INGESTION_RUN_STATUSES}"
        )
    existing = connection.execute(
        "SELECT metadata_json FROM ingestion_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if existing is None:
        raise RevisionStoreError(f"ingestion run is not recorded: {run_id}")
    merged_metadata = _json_value(existing["metadata_json"])
    if not isinstance(merged_metadata, dict):
        merged_metadata = {}
    merged_metadata.update(dict(metadata or {}))
    metadata_json = _json_text(merged_metadata, "metadata")
    connection.execute(
        """
        UPDATE ingestion_runs
        SET completed_at_utc=?, status=?, metadata_json=?
        WHERE run_id=?
        """,
        (_utc_now(), status, metadata_json, run_id),
    )


def start_ingestion_run(
    database: str | Path | None,
    *,
    source: str = "manual",
    metadata: Mapping[str, Any] | None = None,
    started_at_utc: str | None = None,
    run_id: str | None = None,
) -> str:
    target = initialize_revision_store(database)
    with _connect(target) as connection:
        return _create_ingestion_run(
            connection,
            source=source,
            metadata=metadata,
            started_at_utc=started_at_utc,
            run_id=run_id,
        )


create_ingestion_run = start_ingestion_run


def complete_ingestion_run(
    database: str | Path | None,
    run_id: str,
    *,
    status: str = "completed",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    target = initialize_revision_store(database)
    with _connect(target) as connection:
        row = connection.execute(
            "SELECT 1 FROM ingestion_runs WHERE run_id=?",
            (_required_text(run_id, "run_id"),),
        ).fetchone()
        if row is None:
            raise RevisionStoreError(f"ingestion run is not recorded: {run_id}")
        _finish_ingestion_run(connection, run_id, status=status, metadata=metadata)


finish_ingestion_run = complete_ingestion_run


def get_ingestion_run(
    database: str | Path | None,
    run_id: str,
) -> dict[str, Any] | None:
    target = initialize_revision_store(database)
    with _connect(target) as connection:
        row = connection.execute(
            "SELECT * FROM ingestion_runs WHERE run_id=?",
            (_required_text(run_id, "run_id"),),
        ).fetchone()
    return None if row is None else _ingestion_run_row(row)


def list_ingestion_runs(database: str | Path | None = None) -> list[dict[str, Any]]:
    target = initialize_revision_store(database)
    with _connect(target) as connection:
        rows = connection.execute(
            "SELECT * FROM ingestion_runs ORDER BY started_at_utc, run_id"
        ).fetchall()
    return [_ingestion_run_row(row) for row in rows]


def _ingestion_run_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["metadata"] = _json_value(result.pop("metadata_json"))
    return result


def _revision_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["natural_key"] = _json_value(result.pop("natural_key_json"))
    result["record"] = _json_value(result.pop("record_json"))
    result["payload"] = dict(result["record"])
    result["record_type"] = result["dataset"]
    return result


def _natural_key_json(natural_key: Mapping[str, Any]) -> str:
    if not isinstance(natural_key, Mapping) or not natural_key:
        raise RevisionStoreError("natural_key must be a non-empty object")
    return _json_text(dict(natural_key), "natural_key")


class RevisionStore:
    """Append-only SQLite store for canonical data revisions."""

    def __init__(
        self,
        database: str | Path | None = None,
        *,
        initialize: bool = True,
    ) -> None:
        self.database = (
            database_path() if database is None else validate_database_path(database)
        )
        if initialize:
            initialize_revision_store(self.database)

    def initialize(self) -> Path:
        return initialize_revision_store(self.database)

    def start_ingestion_run(
        self,
        *,
        source: str = "manual",
        metadata: Mapping[str, Any] | None = None,
        started_at_utc: str | None = None,
        run_id: str | None = None,
    ) -> str:
        return start_ingestion_run(
            self.database,
            source=source,
            metadata=metadata,
            started_at_utc=started_at_utc,
            run_id=run_id,
        )

    create_ingestion_run = start_ingestion_run

    def complete_ingestion_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        complete_ingestion_run(
            self.database, run_id, status=status, metadata=metadata
        )

    finish_ingestion_run = complete_ingestion_run

    def get_ingestion_run(self, run_id: str) -> dict[str, Any] | None:
        return get_ingestion_run(self.database, run_id)

    def list_ingestion_runs(self) -> list[dict[str, Any]]:
        return list_ingestion_runs(self.database)

    def append_revision(
        self,
        record: Revision | BaseRecord | Mapping[str, Any],
        *,
        dataset: str | None = None,
        natural_key: Mapping[str, Any] | None = None,
        status: RevisionStatus | str = RevisionStatus.OBSERVED,
        source: str = "manual",
        observed_at_utc: str | None = None,
        recorded_at_utc: str | None = None,
        ingestion_run_id: str | None = None,
        supersedes_revision_id: str | None = None,
        revision_id: str | None = None,
        schema_version: int = REVISION_SCHEMA_VERSION,
        run_metadata: Mapping[str, Any] | None = None,
    ) -> str:
        if isinstance(record, Revision):
            resolved_dataset = dataset or record.dataset
            resolved_key = natural_key or record.natural_key
            resolved_status = status
            if status == RevisionStatus.OBSERVED and record.status != RevisionStatus.OBSERVED:
                resolved_status = record.status
            resolved_source = source if source != "manual" else record.source
            resolved_observed = observed_at_utc or record.observed_at_utc
            resolved_recorded = recorded_at_utc or record.recorded_at_utc
            resolved_run = ingestion_run_id or record.ingestion_run_id or None
            resolved_supersedes = (
                supersedes_revision_id
                if supersedes_revision_id is not None
                else record.supersedes_revision_id
            )
            resolved_id = revision_id or record.revision_id
            resolved_schema = schema_version or record.schema_version
            revision = Revision(
                dataset=resolved_dataset,
                natural_key=resolved_key,
                record=record.record,
                status=resolved_status,
                source=resolved_source,
                observed_at_utc=resolved_observed,
                recorded_at_utc=resolved_recorded,
                ingestion_run_id=resolved_run or "",
                supersedes_revision_id=resolved_supersedes,
                revision_id=resolved_id,
                schema_version=resolved_schema,
            )
        else:
            revision = make_revision(
                record,
                dataset=dataset,
                natural_key=natural_key,
                status=status,
                source=source,
                observed_at_utc=observed_at_utc,
                recorded_at_utc=recorded_at_utc,
                ingestion_run_id=ingestion_run_id or "",
                supersedes_revision_id=supersedes_revision_id,
                revision_id=revision_id,
                schema_version=schema_version,
            )

        created_run = False
        with _connect(self.database) as connection:
            _initialize_schema(connection)
            run_id, created_run = _ensure_ingestion_run(
                connection,
                ingestion_run_id=revision.ingestion_run_id,
                source=revision.source,
                metadata=run_metadata,
            )
            if run_id != revision.ingestion_run_id:
                revision = Revision(
                    dataset=revision.dataset,
                    natural_key=revision.natural_key,
                    record=revision.record,
                    status=revision.status,
                    source=revision.source,
                    observed_at_utc=revision.observed_at_utc,
                    recorded_at_utc=revision.recorded_at_utc,
                    ingestion_run_id=run_id,
                    supersedes_revision_id=revision.supersedes_revision_id,
                    revision_id=revision.revision_id,
                    schema_version=revision.schema_version,
                )

            existing = connection.execute(
                "SELECT * FROM data_revisions WHERE revision_id=?",
                (revision.revision_id,),
            ).fetchone()
            if existing is not None:
                if created_run:
                    _finish_ingestion_run(
                        connection,
                        run_id,
                        status="completed",
                        metadata={"revision_id": revision.revision_id, "idempotent": True},
                    )
                return str(revision.revision_id)

            if revision.supersedes_revision_id is not None:
                superseded = connection.execute(
                    """
                    SELECT dataset, natural_key_json, revision_id
                    FROM data_revisions
                    WHERE revision_id=?
                    """,
                    (revision.supersedes_revision_id,),
                ).fetchone()
                if superseded is None:
                    raise RevisionStoreError(
                        "supersedes_revision_id is not recorded: "
                        f"{revision.supersedes_revision_id}"
                    )
                if superseded["revision_id"] == revision.revision_id:
                    raise RevisionStoreError("a revision cannot supersede itself")
                if (
                    superseded["dataset"] != revision.dataset
                    or superseded["natural_key_json"]
                    != _natural_key_json(revision.natural_key)
                ):
                    raise RevisionStoreError(
                        "superseded revision must have the same dataset and natural key"
                    )

            connection.execute(
                """
                INSERT INTO data_revisions (
                    revision_id, schema_version, dataset, natural_key_json,
                    status, record_json, source, observed_at_utc,
                    recorded_at_utc, ingestion_run_id, supersedes_revision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision.revision_id,
                    revision.schema_version,
                    revision.dataset,
                    _natural_key_json(revision.natural_key),
                    revision.status.value,
                    _json_text(revision.record, "record"),
                    revision.source,
                    revision.observed_at_utc,
                    revision.recorded_at_utc,
                    run_id,
                    revision.supersedes_revision_id,
                ),
            )
            if created_run:
                _finish_ingestion_run(
                    connection,
                    run_id,
                    status="completed",
                    metadata={"revision_id": revision.revision_id, "idempotent": False},
                )
        return str(revision.revision_id)

    def append_revisions(
        self,
        records: Any,
        *,
        dataset: str,
        natural_keys: Any = None,
        status: RevisionStatus | str = RevisionStatus.OBSERVED,
        source: str = "manual",
        observed_at_utc: str | None = None,
        recorded_at_utc: str | None = None,
        ingestion_run_id: str,
        schema_version: int = REVISION_SCHEMA_VERSION,
        run_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a large normalized batch in one SQLite transaction."""
        resolved_dataset = normalize_dataset(dataset)
        record_list = list(records)
        key_list = (
            list(natural_keys)
            if natural_keys is not None
            else [None] * len(record_list)
        )
        if len(key_list) != len(record_list):
            raise RevisionStoreError("records and natural_keys must have the same length")
        inserted = 0
        skipped = 0
        inserted_ids: list[str] = []
        with _connect(self.database) as connection:
            _initialize_schema(connection)
            for record, natural_key in zip(record_list, key_list):
                revision = make_revision(
                    record,
                    dataset=resolved_dataset,
                    natural_key=natural_key,
                    status=status,
                    source=source,
                    observed_at_utc=observed_at_utc,
                    recorded_at_utc=recorded_at_utc,
                    ingestion_run_id=ingestion_run_id,
                    schema_version=schema_version,
                )
                existing = connection.execute(
                    "SELECT 1 FROM data_revisions WHERE revision_id=?",
                    (revision.revision_id,),
                ).fetchone()
                if existing is not None:
                    skipped += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO data_revisions (
                        revision_id, schema_version, dataset, natural_key_json,
                        status, record_json, source, observed_at_utc,
                        recorded_at_utc, ingestion_run_id, supersedes_revision_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision.revision_id,
                        revision.schema_version,
                        revision.dataset,
                        _natural_key_json(revision.natural_key),
                        revision.status.value,
                        _json_text(revision.record, "record"),
                        revision.source,
                        revision.observed_at_utc,
                        revision.recorded_at_utc,
                        ingestion_run_id,
                        revision.supersedes_revision_id,
                    ),
                )
                inserted += 1
                inserted_ids.append(str(revision.revision_id))
        return {
            "inserted_count": inserted,
            "skipped_count": skipped,
            "revision_ids": inserted_ids,
            "run_metadata": dict(run_metadata or {}),
        }

    record_revision = append_revision
    append = append_revision

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        return get_revision(self.database, revision_id)

    def list_revisions(
        self,
        *,
        dataset: str | None = None,
        natural_key: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return list_revisions(
            self.database, dataset=dataset, natural_key=natural_key
        )

    def history(
        self,
        dataset: str,
        natural_key: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        return self.list_revisions(dataset=dataset, natural_key=natural_key)

    def current_revision(
        self,
        dataset: str,
        natural_key: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        return current_revision(self.database, dataset, natural_key)

    def current_record(
        self,
        dataset: str,
        natural_key: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        return self.current_revision(dataset, natural_key)

    get_current = current_revision

    def current(
        self,
        dataset: str,
        natural_key: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        return self.current_revision(dataset, natural_key)

    latest_revision = current_revision

    def current_records(self, dataset: str | None = None) -> list[dict[str, Any]]:
        return current_records(self.database, dataset=dataset)

    def count_revisions(self) -> int:
        return count_revisions(self.database)


def get_revision(
    database: str | Path | None,
    revision_id: str,
) -> dict[str, Any] | None:
    target = initialize_revision_store(database)
    with _connect(target) as connection:
        row = connection.execute(
            "SELECT * FROM revision_history WHERE revision_id=?",
            (_required_text(revision_id, "revision_id"),),
        ).fetchone()
    return None if row is None else _revision_row(row)


def list_revisions(
    database: str | Path | None = None,
    *,
    dataset: str | None = None,
    natural_key: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    target = initialize_revision_store(database)
    clauses: list[str] = []
    params: list[Any] = []
    if dataset is not None:
        clauses.append("dataset=?")
        params.append(normalize_dataset(dataset))
    if natural_key is not None:
        clauses.append("natural_key_json=?")
        params.append(_natural_key_json(natural_key))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect(target) as connection:
        rows = connection.execute(
            f"SELECT * FROM revision_history{where} ORDER BY revision_sequence DESC",
            params,
        ).fetchall()
    return [_revision_row(row) for row in rows]


def current_revision(
    database: str | Path | None,
    dataset: str,
    natural_key: Mapping[str, Any],
) -> dict[str, Any] | None:
    target = initialize_revision_store(database)
    resolved_dataset = normalize_dataset(dataset)
    with _connect(target) as connection:
        row = connection.execute(
            """
            SELECT * FROM current_revisions
            WHERE dataset=? AND natural_key_json=?
            ORDER BY revision_sequence DESC
            LIMIT 1
            """,
            (resolved_dataset, _natural_key_json(natural_key)),
        ).fetchone()
    return None if row is None else _revision_row(row)


def current_records(
    database: str | Path | None = None,
    *,
    dataset: str | None = None,
) -> list[dict[str, Any]]:
    target = initialize_revision_store(database)
    if dataset is None:
        sql = "SELECT * FROM current_revisions ORDER BY dataset, natural_key_json"
        params: tuple[Any, ...] = ()
    else:
        resolved_dataset = normalize_dataset(dataset)
        view_name = DATASET_CURRENT_VIEWS[resolved_dataset]
        sql = f"SELECT * FROM {view_name} ORDER BY natural_key_json"
        params = ()
    with _connect(target) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [_revision_row(row) for row in rows]


def count_revisions(database: str | Path | None = None) -> int:
    target = initialize_revision_store(database)
    with _connect(target) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM data_revisions").fetchone()[0])


def append_revision(
    database: str | Path | None,
    record: Revision | BaseRecord | Mapping[str, Any],
    **kwargs: Any,
) -> str:
    return RevisionStore(database).append_revision(record, **kwargs)


record_revision = append_revision
insert_revision = append_revision
write_revision = append_revision
latest_revision = current_revision


__all__ = [
    "DATASET_CURRENT_VIEWS",
    "INGESTION_RUN_STATUSES",
    "SCHEMA_VERSION",
    "RevisionStore",
    "RevisionStoreError",
    "append_revision",
    "complete_ingestion_run",
    "count_revisions",
    "create_ingestion_run",
    "current_records",
    "current_revision",
    "finish_ingestion_run",
    "get_ingestion_run",
    "get_revision",
    "initialize_revision_store",
    "initialize_store",
    "insert_revision",
    "latest_revision",
    "list_ingestion_runs",
    "list_revisions",
    "record_revision",
    "start_ingestion_run",
    "write_revision",
]
