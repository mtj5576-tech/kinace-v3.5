from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import database_path, validate_database_path
from .feature_snapshot import (
    FeatureSnapshotError,
    canonical_json_bytes,
    initialize_feature_snapshot_schema,
    sha256_file,
)
from .score_replay import (
    ScoreReplayError,
    candidate_id_for,
    validate_candidate_config,
)


SCHEMA_VERSION = 1


class ExperimentLineageError(ValueError):
    """Raised when an experiment lineage record is invalid or incomplete."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentLineageError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentLineageError(f"{field} must be a non-empty string")
    return value


def _json_text(value: Any, field: str) -> str:
    try:
        return canonical_json_bytes(value, trailing_newline=False).decode("utf-8")
    except FeatureSnapshotError as exc:
        raise ExperimentLineageError(f"{field} is not valid JSON: {exc}") from exc


def _json_value(value: str) -> Any:
    return json.loads(value)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(target: Path) -> sqlite3.Connection:
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS model_candidates (
            candidate_id TEXT PRIMARY KEY,
            code_version TEXT NOT NULL,
            candidate_config_json TEXT NOT NULL,
            candidate_config_sha256 TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS score_replays (
            replay_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES model_candidates(candidate_id),
            snapshot_id TEXT NOT NULL,
            data_snapshot_hash TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL UNIQUE,
            candidate_config_json TEXT NOT NULL,
            source_snapshot_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id TEXT PRIMARY KEY,
            code_version TEXT NOT NULL,
            data_snapshot_hash TEXT NOT NULL,
            search_space_json TEXT NOT NULL,
            seed INTEGER NOT NULL,
            objective_config_json TEXT NOT NULL,
            candidate_id TEXT NOT NULL REFERENCES model_candidates(candidate_id),
            replay_id TEXT NOT NULL REFERENCES score_replays(replay_id),
            artifact_paths_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS backtest_evidence (
            backtest_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            code_version TEXT NOT NULL,
            candidate_id TEXT NOT NULL REFERENCES model_candidates(candidate_id),
            replay_id TEXT NOT NULL REFERENCES score_replays(replay_id),
            metrics_json TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_score_replays_candidate
            ON score_replays(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_experiments_candidate_replay
            ON experiments(candidate_id, replay_id);
        CREATE INDEX IF NOT EXISTS idx_backtest_candidate_replay
            ON backtest_evidence(candidate_id, replay_id);
        """
    )
    initialize_feature_snapshot_schema(connection)
    connection.execute(
        """
        INSERT INTO schema_metadata (key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )


def initialize_experiments_database(
    database: str | Path | None = None,
) -> Path:
    target = database_path() if database is None else validate_database_path(database)
    with _connect(target) as connection:
        _initialize_schema(connection)
    return target


def record_candidate(
    database: str | Path | None,
    *,
    code_version: str,
    candidate_config: Mapping[str, Any],
    candidate_id: str | None = None,
) -> str:
    target = initialize_experiments_database(database)
    candidate = validate_candidate_config(candidate_config)
    expected_id = candidate_id_for(candidate)
    resolved_id = expected_id if candidate_id is None else _required_text(candidate_id, "candidate_id")
    if resolved_id != expected_id:
        raise ExperimentLineageError(
            f"candidate_id does not match candidate configuration: {resolved_id} != {expected_id}"
        )
    config_json = _json_text(candidate, "candidate_config")
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO model_candidates (
                candidate_id, code_version, candidate_config_json,
                candidate_config_sha256, created_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                code_version=excluded.code_version,
                candidate_config_json=excluded.candidate_config_json,
                candidate_config_sha256=excluded.candidate_config_sha256
            """,
            (
                resolved_id,
                _required_text(code_version, "code_version"),
                config_json,
                hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
                _now_utc(),
            ),
        )
    return resolved_id


def record_replay(
    database: str | Path | None,
    replay: Mapping[str, Any],
    *,
    code_version: str,
) -> str:
    target = initialize_experiments_database(database)
    payload = _mapping(replay.get("payload"), "replay.payload")
    replay_id = _required_text(replay.get("replay_id"), "replay_id")
    artifact_path = _required_text(replay.get("artifact_path"), "artifact_path")
    artifact_sha256 = _required_text(replay.get("artifact_sha256"), "artifact_sha256")
    if replay_id != artifact_sha256:
        raise ExperimentLineageError("replay_id and artifact_sha256 must match")
    if sha256_file(artifact_path) != artifact_sha256:
        raise ExperimentLineageError("replay artifact hash does not match the artifact file")
    if payload.get("artifact_type") != "kinace_v3_score_replay":
        raise ExperimentLineageError("replay payload has the wrong artifact type")

    candidate_config = _mapping(payload.get("candidate_config"), "candidate_config")
    candidate_id = _required_text(payload.get("candidate_id"), "candidate_id")
    expected_candidate_id = candidate_id_for(candidate_config)
    if candidate_id != expected_candidate_id:
        raise ExperimentLineageError("replay candidate_id does not match candidate configuration")
    source_snapshot = _mapping(payload.get("source_snapshot"), "source_snapshot")
    snapshot_id = _required_text(source_snapshot.get("snapshot_id"), "snapshot_id")
    data_snapshot_hash = _required_text(
        source_snapshot.get("artifact_sha256"), "source_snapshot.artifact_sha256"
    )

    record_candidate(
        target,
        code_version=code_version,
        candidate_config=candidate_config,
        candidate_id=candidate_id,
    )
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO score_replays (
                replay_id, candidate_id, snapshot_id, data_snapshot_hash,
                artifact_path, artifact_sha256, candidate_config_json,
                source_snapshot_json, schema_version, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(replay_id) DO UPDATE SET
                candidate_id=excluded.candidate_id,
                snapshot_id=excluded.snapshot_id,
                data_snapshot_hash=excluded.data_snapshot_hash,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                candidate_config_json=excluded.candidate_config_json,
                source_snapshot_json=excluded.source_snapshot_json,
                schema_version=excluded.schema_version
            """,
            (
                replay_id,
                candidate_id,
                snapshot_id,
                data_snapshot_hash,
                artifact_path,
                artifact_sha256,
                _json_text(candidate_config, "candidate_config"),
                _json_text(source_snapshot, "source_snapshot"),
                int(payload.get("schema_version") or SCHEMA_VERSION),
                _now_utc(),
            ),
        )
    return replay_id


def experiment_id_for(
    *,
    code_version: str,
    data_snapshot_hash: str,
    search_space: Mapping[str, Any],
    seed: int,
    objective_config: Mapping[str, Any],
    candidate_id: str,
    replay_id: str,
    artifact_paths: Mapping[str, Any],
) -> str:
    identity = {
        "code_version": code_version,
        "data_snapshot_hash": data_snapshot_hash,
        "search_space": deepcopy(dict(search_space)),
        "seed": int(seed),
        "objective_config": deepcopy(dict(objective_config)),
        "candidate_id": candidate_id,
        "replay_id": replay_id,
        "artifact_paths": deepcopy(dict(artifact_paths)),
    }
    return hashlib.sha256(
        canonical_json_bytes(identity, trailing_newline=False)
    ).hexdigest()


def record_experiment(
    database: str | Path | None,
    *,
    code_version: str,
    data_snapshot_hash: str,
    search_space: Mapping[str, Any],
    seed: int,
    objective_config: Mapping[str, Any],
    candidate_id: str,
    replay_id: str,
    artifact_paths: Mapping[str, Any],
) -> str:
    target = initialize_experiments_database(database)
    search = deepcopy(dict(_mapping(search_space, "search_space")))
    objective = deepcopy(dict(_mapping(objective_config, "objective_config")))
    paths = deepcopy(dict(_mapping(artifact_paths, "artifact_paths")))
    normalized_paths = {
        str(key): str(Path(value).resolve(strict=False)) if isinstance(value, (str, Path)) else value
        for key, value in paths.items()
    }
    with _connect(target) as connection:
        replay = connection.execute(
            "SELECT candidate_id, data_snapshot_hash FROM score_replays WHERE replay_id=?",
            (_required_text(replay_id, "replay_id"),),
        ).fetchone()
        if replay is None:
            raise ExperimentLineageError(f"replay_id is not recorded: {replay_id}")
        if replay["candidate_id"] != candidate_id:
            raise ExperimentLineageError("candidate_id does not match replay lineage")
        if replay["data_snapshot_hash"] != data_snapshot_hash:
            raise ExperimentLineageError("data_snapshot_hash does not match replay lineage")

        experiment_id = experiment_id_for(
            code_version=code_version,
            data_snapshot_hash=data_snapshot_hash,
            search_space=search,
            seed=seed,
            objective_config=objective,
            candidate_id=candidate_id,
            replay_id=replay_id,
            artifact_paths=normalized_paths,
        )
        connection.execute(
            """
            INSERT INTO experiments (
                experiment_id, code_version, data_snapshot_hash,
                search_space_json, seed, objective_config_json,
                candidate_id, replay_id, artifact_paths_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id) DO UPDATE SET
                code_version=excluded.code_version,
                data_snapshot_hash=excluded.data_snapshot_hash,
                search_space_json=excluded.search_space_json,
                seed=excluded.seed,
                objective_config_json=excluded.objective_config_json,
                candidate_id=excluded.candidate_id,
                replay_id=excluded.replay_id,
                artifact_paths_json=excluded.artifact_paths_json
            """,
            (
                experiment_id,
                _required_text(code_version, "code_version"),
                _required_text(data_snapshot_hash, "data_snapshot_hash"),
                _json_text(search, "search_space"),
                int(seed),
                _json_text(objective, "objective_config"),
                _required_text(candidate_id, "candidate_id"),
                _required_text(replay_id, "replay_id"),
                _json_text(normalized_paths, "artifact_paths"),
                _now_utc(),
            ),
        )
    return experiment_id


def record_backtest_evidence(
    database: str | Path | None,
    *,
    code_version: str,
    candidate_id: str,
    replay_id: str,
    run_id: str,
    metrics: Mapping[str, Any],
    artifact_path: str | Path,
    artifact_sha256: str | None = None,
    backtest_id: str | None = None,
) -> str:
    target = initialize_experiments_database(database)
    metric_json = _json_text(_mapping(metrics, "metrics"), "metrics")
    path = Path(artifact_path).resolve(strict=False)
    actual_sha256 = sha256_file(path)
    resolved_sha256 = (
        actual_sha256
        if artifact_sha256 is None
        else _required_text(artifact_sha256, "artifact_sha256")
    )
    if actual_sha256 != resolved_sha256:
        raise ExperimentLineageError("backtest artifact hash does not match the artifact file")
    identity = {
        "run_id": _required_text(run_id, "run_id"),
        "candidate_id": _required_text(candidate_id, "candidate_id"),
        "replay_id": _required_text(replay_id, "replay_id"),
        "metrics": json.loads(metric_json),
        "artifact_path": str(path),
        "artifact_sha256": resolved_sha256,
    }
    resolved_backtest_id = (
        hashlib.sha256(
            canonical_json_bytes(identity, trailing_newline=False)
        ).hexdigest()
        if backtest_id is None
        else _required_text(backtest_id, "backtest_id")
    )
    with _connect(target) as connection:
        replay = connection.execute(
            "SELECT candidate_id FROM score_replays WHERE replay_id=?",
            (identity["replay_id"],),
        ).fetchone()
        if replay is None or replay["candidate_id"] != identity["candidate_id"]:
            raise ExperimentLineageError("backtest evidence does not match replay lineage")
        connection.execute(
            "DELETE FROM backtest_evidence WHERE run_id=? AND backtest_id<>?",
            (identity["run_id"], resolved_backtest_id),
        )
        connection.execute(
            """
            INSERT INTO backtest_evidence (
                backtest_id, run_id, code_version, candidate_id, replay_id,
                metrics_json, artifact_path, artifact_sha256, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(backtest_id) DO UPDATE SET
                run_id=excluded.run_id,
                code_version=excluded.code_version,
                candidate_id=excluded.candidate_id,
                replay_id=excluded.replay_id,
                metrics_json=excluded.metrics_json,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256
            """,
            (
                resolved_backtest_id,
                identity["run_id"],
                _required_text(code_version, "code_version"),
                identity["candidate_id"],
                identity["replay_id"],
                metric_json,
                str(path),
                resolved_sha256,
                _now_utc(),
            ),
        )
    return resolved_backtest_id


def join_lineage(
    database: str | Path | None,
    *,
    experiment_id: str | None = None,
    candidate_id: str | None = None,
    replay_id: str | None = None,
    backtest_id: str | None = None,
) -> list[dict[str, Any]]:
    target = initialize_experiments_database(database)
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("e.experiment_id", experiment_id),
        ("c.candidate_id", candidate_id),
        ("r.replay_id", replay_id),
        ("b.backtest_id", backtest_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT
            e.experiment_id, e.code_version AS experiment_code_version,
            e.data_snapshot_hash, e.search_space_json, e.seed,
            e.objective_config_json, e.artifact_paths_json,
            e.created_at_utc AS experiment_created_at_utc,
            c.candidate_id, c.code_version AS candidate_code_version,
            c.candidate_config_json, c.candidate_config_sha256,
            r.replay_id, r.snapshot_id, r.artifact_path AS replay_artifact_path,
            r.artifact_sha256 AS replay_artifact_sha256,
            r.source_snapshot_json,
            b.backtest_id, b.run_id, b.metrics_json,
            b.artifact_path AS backtest_artifact_path,
            b.artifact_sha256 AS backtest_artifact_sha256
        FROM experiments e
        JOIN model_candidates c ON c.candidate_id=e.candidate_id
        JOIN score_replays r ON r.replay_id=e.replay_id
        LEFT JOIN backtest_evidence b
            ON b.candidate_id=e.candidate_id AND b.replay_id=e.replay_id
        {where}
        ORDER BY e.created_at_utc, e.experiment_id, b.backtest_id
    """
    with _connect(target) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [
        {
            "experiment": {
                "experiment_id": row["experiment_id"],
                "code_version": row["experiment_code_version"],
                "data_snapshot_hash": row["data_snapshot_hash"],
                "search_space": _json_value(row["search_space_json"]),
                "seed": row["seed"],
                "objective_config": _json_value(row["objective_config_json"]),
                "artifact_paths": _json_value(row["artifact_paths_json"]),
                "created_at_utc": row["experiment_created_at_utc"],
            },
            "candidate": {
                "candidate_id": row["candidate_id"],
                "code_version": row["candidate_code_version"],
                "candidate_config": _json_value(row["candidate_config_json"]),
                "candidate_config_sha256": row["candidate_config_sha256"],
            },
            "replay": {
                "replay_id": row["replay_id"],
                "snapshot_id": row["snapshot_id"],
                "artifact_path": row["replay_artifact_path"],
                "artifact_sha256": row["replay_artifact_sha256"],
                "source_snapshot": _json_value(row["source_snapshot_json"]),
            },
            "backtest": (
                None
                if row["backtest_id"] is None
                else {
                    "backtest_id": row["backtest_id"],
                    "run_id": row["run_id"],
                    "metrics": _json_value(row["metrics_json"]),
                    "artifact_path": row["backtest_artifact_path"],
                    "artifact_sha256": row["backtest_artifact_sha256"],
                }
            ),
        }
        for row in rows
    ]


__all__ = [
    "SCHEMA_VERSION",
    "ExperimentLineageError",
    "experiment_id_for",
    "initialize_experiments_database",
    "join_lineage",
    "record_backtest_evidence",
    "record_candidate",
    "record_experiment",
    "record_replay",
]
