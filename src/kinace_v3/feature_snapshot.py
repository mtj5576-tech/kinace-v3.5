from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import database_path, validate_database_path
from .research_weighting import normalize_standardization_config, standardize_values


SCHEMA_VERSION = 1
FEATURE_NAMES = (
    "rel_sector_strength",
    "close_location_5",
    "volume_ratio",
    "log_amount",
    "turnover",
    "proximity_20d_high",
    "ret_5",
    "gap",
)
MODEL_PENALTY_NAMES = (
    "rsi_overbought",
    "kdj_overbought",
    "overheat_penalty",
    "amplitude_threshold",
    "amplitude_multiplier",
    "amplitude_max_penalty",
)
PENALTY_INPUT_NAMES = ("rsi", "kdj_j", "amplitude_5d")


class FeatureSnapshotError(ValueError):
    """Raised when a point-in-time feature snapshot is invalid."""


def canonical_json_bytes(payload: Any, *, trailing_newline: bool = True) -> bytes:
    """Return deterministic UTF-8 JSON suitable for hashing and persistence."""
    try:
        text = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FeatureSnapshotError(f"payload is not canonical JSON: {exc}") from exc
    if trailing_newline:
        text += "\n"
    return text.encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FeatureSnapshotError(f"{field} must be an object")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeatureSnapshotError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise FeatureSnapshotError(f"{field} must be a finite number")
    return number


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, field)


def validate_model_config(model_config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and preserve the exact model configuration supplied by the caller."""
    source = _mapping(model_config, "model_config")
    result = deepcopy(dict(source))
    version = result.get("version")
    if not isinstance(version, str) or not version.strip():
        raise FeatureSnapshotError("model_config.version must be a non-empty string")

    weights = _mapping(result.get("weights"), "model_config.weights")
    clean_weights = deepcopy(dict(weights))
    clean_weights.update({
        name: _finite_number(weights.get(name), f"model_config.weights.{name}")
        for name in FEATURE_NAMES
    })
    if any(clean_weights[name] < 0 for name in FEATURE_NAMES):
        raise FeatureSnapshotError("model_config weights cannot be negative")
    if sum(clean_weights[name] for name in FEATURE_NAMES) <= 0:
        raise FeatureSnapshotError("model_config weights must have a positive sum")
    result["weights"] = clean_weights
    result["standardization"] = normalize_standardization_config(
        result.get("standardization")
    )
    raw_directions = result.get("feature_directions")
    if raw_directions is None:
        directions = {name: 1 for name in FEATURE_NAMES}
    else:
        supplied = _mapping(raw_directions, "model_config.feature_directions")
        unknown_directions = sorted(set(supplied) - set(FEATURE_NAMES))
        if unknown_directions:
            raise FeatureSnapshotError(
                "unknown feature directions: " + ", ".join(unknown_directions)
            )
        directions = {}
        for name in FEATURE_NAMES:
            value = supplied.get(name, 1)
            if value not in {-1, 1}:
                raise FeatureSnapshotError(
                    f"model_config.feature_directions.{name} must be -1 or 1"
                )
            directions[name] = int(value)
    result["feature_directions"] = directions

    penalties = _mapping(result.get("penalties"), "model_config.penalties")
    clean_penalties = deepcopy(dict(penalties))
    clean_penalties.update({
        name: _finite_number(penalties.get(name), f"model_config.penalties.{name}")
        for name in MODEL_PENALTY_NAMES
    })
    result["penalties"] = clean_penalties
    return result


def _raw_inputs(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source = candidate.get("raw_inputs")
    raw = dict(_mapping(source, "candidate.raw_inputs")) if source is not None else {}
    result: dict[str, Any] = {}
    for name in FEATURE_NAMES:
        result[name] = _optional_number(raw.get(name), f"raw_inputs.{name}")
    for name, value in raw.items():
        if name not in result:
            result[str(name)] = deepcopy(value)
    return result


def _penalty_inputs(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source = candidate.get("penalty_inputs")
    supplied = dict(_mapping(source, "candidate.penalty_inputs")) if source is not None else {}
    if "kdj_j" not in supplied and isinstance(candidate.get("kdj"), Mapping):
        supplied["kdj_j"] = candidate["kdj"].get("j")
    result = {
        name: _optional_number(supplied.get(name), f"penalty_inputs.{name}")
        for name in PENALTY_INPUT_NAMES
    }
    missing = [name for name in PENALTY_INPUT_NAMES if result[name] is None]
    result["missing_fields"] = missing
    return result


def _point_in_time(candidate: Mapping[str, Any], decision_date: str) -> dict[str, Any]:
    source = candidate.get("point_in_time")
    point_in_time = dict(_mapping(source, "candidate.point_in_time")) if source is not None else {}
    point_in_time["decision_date"] = decision_date
    if "as_of_utc" in candidate and "as_of_utc" not in point_in_time:
        point_in_time["as_of_utc"] = candidate["as_of_utc"]
    return point_in_time


def _standardization_inputs(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source = candidate.get("standardization_inputs")
    values = dict(_mapping(source, "candidate.standardization_inputs")) if source else {}
    industry = values.get("industry", candidate.get("industry"))
    market_cap = values.get("market_cap", candidate.get("market_cap"))
    return {
        "industry": str(industry) if industry not in {None, ""} else None,
        "market_cap": (
            _optional_number(market_cap, "standardization_inputs.market_cap")
            if market_cap is not None
            else None
        ),
    }


def compute_z_score_inputs(
    raw_rows: Sequence[Mapping[str, Any]],
    weights: Mapping[str, Any],
    standardization: Mapping[str, Any] | None = None,
) -> list[dict[str, float | None]]:
    """Standardize raw cross-sectional inputs with one explicit configuration."""
    result: list[dict[str, float | None]] = [dict() for _ in raw_rows]
    for name in FEATURE_NAMES:
        available = [
            (index, float(row["raw_inputs"][name]))
            for index, row in enumerate(raw_rows)
            if row["raw_inputs"].get(name) is not None
        ]
        if not available:
            for row in result:
                row[name] = 0.0 if float(weights.get(name, 0.0)) == 0.0 else None
            continue
        values = [value for _, value in available]
        industries = None
        market_caps = None
        if standardization is not None:
            settings = normalize_standardization_config(standardization)
            if settings["neutralize_industry"] or settings["neutralize_market_cap"]:
                industries = [
                    str(
                        (raw_rows[index].get("standardization_inputs") or {}).get(
                            "industry"
                        )
                        or "__unavailable__"
                    )
                    for index, _ in available
                ]
            if settings["neutralize_market_cap"]:
                market_caps = [
                    _finite_number(
                        (raw_rows[index].get("standardization_inputs") or {}).get(
                            "market_cap"
                        ),
                        f"standardization_inputs.market_cap[{index}]",
                    )
                    for index, _ in available
                ]
        standardized = standardize_values(
            values,
            config=standardization,
            industries=industries,
            market_caps=market_caps,
        )
        standardized_by_index = {
            index: standardized[position]
            for position, (index, _) in enumerate(available)
        }
        for index in range(len(result)):
            value = raw_rows[index]["raw_inputs"].get(name)
            result[index][name] = (
                0.0 if value is None else standardized_by_index.get(index, 0.0)
            )
    return result


def create_feature_snapshot(
    *,
    decision_date: str,
    candidates: Sequence[Mapping[str, Any]],
    model_config: Mapping[str, Any],
    data_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, point-in-time feature snapshot payload."""
    if not isinstance(decision_date, str) or not decision_date.strip():
        raise FeatureSnapshotError("decision_date must be a non-empty string")
    exact_model_config = validate_model_config(model_config)

    normalized: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for index, candidate in enumerate(candidates):
        source = _mapping(candidate, f"candidates[{index}]")
        code = source.get("code")
        if not isinstance(code, str) or not code.strip():
            raise FeatureSnapshotError(f"candidates[{index}].code must be a non-empty string")
        if code in seen_codes:
            raise FeatureSnapshotError(f"duplicate candidate code in snapshot: {code}")
        seen_codes.add(code)
        labels = source.get("labels", {})
        normalized.append(
            {
                "code": code,
                "decision_date": decision_date,
                "point_in_time": _point_in_time(source, decision_date),
                "raw_inputs": _raw_inputs(source),
                "standardization_inputs": _standardization_inputs(source),
                "penalty_inputs": _penalty_inputs(source),
                "labels": deepcopy(dict(_mapping(labels, f"candidates[{index}].labels"))),
                "model_config": deepcopy(exact_model_config),
            }
        )

    normalized.sort(key=lambda row: row["code"])
    z_scores = compute_z_score_inputs(
        normalized,
        exact_model_config["weights"],
        exact_model_config["standardization"],
    )
    rows = []
    for row, z_score_inputs in zip(normalized, z_scores):
        replay_ready = all(z_score_inputs[name] is not None for name in FEATURE_NAMES)
        replay_ready = replay_ready and not row["penalty_inputs"]["missing_fields"]
        rows.append(
            {
                **row,
                "z_score_inputs": z_score_inputs,
                "replay_ready": replay_ready,
            }
        )

    gate = (
        deepcopy(dict(data_gate))
        if data_gate is not None
        else {"status": "unknown", "ready": False}
    )
    gate["parity_claimed"] = False
    return {
        "artifact_type": "kinace_v3_feature_snapshot",
        "schema_version": SCHEMA_VERSION,
        "decision_date": decision_date,
        "row_count": len(rows),
        "model_config": exact_model_config,
        "data_gate": gate,
        "rows": rows,
    }


def write_feature_snapshot(
    payload: Mapping[str, Any],
    output_path: str | Path,
) -> dict[str, Any]:
    """Write a canonical snapshot artifact and return its content address."""
    serialized = canonical_json_bytes(payload)
    target = Path(output_path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(serialized)
    temporary.replace(target)
    digest = sha256_bytes(serialized)
    return {
        "snapshot_id": digest,
        "artifact_sha256": digest,
        "artifact_path": str(target),
        "payload": deepcopy(dict(payload)),
    }


def load_feature_snapshot_artifact(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeatureSnapshotError(f"cannot load feature snapshot artifact: {exc}") from exc
    if not isinstance(payload, dict):
        raise FeatureSnapshotError("feature snapshot artifact root must be an object")
    if payload.get("artifact_type") != "kinace_v3_feature_snapshot":
        raise FeatureSnapshotError("artifact is not a Kinace V3 feature snapshot")
    return payload


def initialize_feature_snapshot_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS feature_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            decision_date TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            model_config_json TEXT NOT NULL,
            data_gate_json TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            UNIQUE(artifact_sha256)
        );

        CREATE TABLE IF NOT EXISTS feature_snapshot_rows (
            snapshot_id TEXT NOT NULL REFERENCES feature_snapshots(snapshot_id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            decision_date TEXT NOT NULL,
            point_in_time_json TEXT NOT NULL,
            raw_inputs_json TEXT NOT NULL,
            z_score_inputs_json TEXT NOT NULL,
            penalty_inputs_json TEXT NOT NULL,
            labels_json TEXT NOT NULL,
            model_config_json TEXT NOT NULL,
            replay_ready INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, code)
        );
        """
    )


def persist_feature_snapshot(
    snapshot: Mapping[str, Any],
    database: str | Path | None = None,
) -> str:
    """Persist a written feature snapshot and all row-level inputs."""
    payload = _mapping(snapshot.get("payload"), "snapshot.payload")
    snapshot_id = snapshot.get("snapshot_id")
    artifact_path = snapshot.get("artifact_path")
    artifact_sha256 = snapshot.get("artifact_sha256")
    if not all(
        isinstance(value, str) and value
        for value in (snapshot_id, artifact_path, artifact_sha256)
    ):
        raise FeatureSnapshotError("snapshot ID and artifact path/hash are required")
    if sha256_file(artifact_path) != artifact_sha256:
        raise FeatureSnapshotError("snapshot artifact hash does not match the artifact file")
    if payload.get("artifact_type") != "kinace_v3_feature_snapshot":
        raise FeatureSnapshotError("snapshot payload has the wrong artifact type")

    target = database_path() if database is None else validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(target)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_feature_snapshot_schema(connection)
        connection.execute(
            """
            INSERT INTO feature_snapshots (
                snapshot_id, schema_version, decision_date, artifact_path,
                artifact_sha256, model_config_json, data_gate_json, row_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                decision_date=excluded.decision_date,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                model_config_json=excluded.model_config_json,
                data_gate_json=excluded.data_gate_json,
                row_count=excluded.row_count
            """,
            (
                snapshot_id,
                int(payload.get("schema_version", SCHEMA_VERSION)),
                str(payload.get("decision_date") or ""),
                artifact_path,
                artifact_sha256,
                canonical_json_bytes(
                    payload.get("model_config") or {}, trailing_newline=False
                ).decode("utf-8"),
                canonical_json_bytes(
                    payload.get("data_gate") or {}, trailing_newline=False
                ).decode("utf-8"),
                int(payload.get("row_count") or 0),
            ),
        )
        connection.execute(
            "DELETE FROM feature_snapshot_rows WHERE snapshot_id=?", (snapshot_id,)
        )
        for row in payload.get("rows") or []:
            row_mapping = _mapping(row, "snapshot row")
            connection.execute(
                """
                INSERT INTO feature_snapshot_rows (
                    snapshot_id, code, decision_date, point_in_time_json,
                    raw_inputs_json, z_score_inputs_json, penalty_inputs_json,
                    labels_json, model_config_json, replay_ready
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    str(row_mapping.get("code") or ""),
                    str(row_mapping.get("decision_date") or ""),
                    canonical_json_bytes(
                        row_mapping.get("point_in_time") or {}, trailing_newline=False
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        row_mapping.get("raw_inputs") or {}, trailing_newline=False
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        row_mapping.get("z_score_inputs") or {}, trailing_newline=False
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        row_mapping.get("penalty_inputs") or {}, trailing_newline=False
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        row_mapping.get("labels") or {}, trailing_newline=False
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        row_mapping.get("model_config") or {}, trailing_newline=False
                    ).decode("utf-8"),
                    int(bool(row_mapping.get("replay_ready"))),
                ),
            )
    return str(snapshot_id)


__all__ = [
    "FEATURE_NAMES",
    "MODEL_PENALTY_NAMES",
    "PENALTY_INPUT_NAMES",
    "SCHEMA_VERSION",
    "FeatureSnapshotError",
    "canonical_json_bytes",
    "compute_z_score_inputs",
    "create_feature_snapshot",
    "initialize_feature_snapshot_schema",
    "load_feature_snapshot_artifact",
    "persist_feature_snapshot",
    "sha256_bytes",
    "sha256_file",
    "validate_model_config",
    "write_feature_snapshot",
]
