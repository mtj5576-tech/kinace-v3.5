from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .config import (
    DEFAULT_DATABASE_PATH,
    PROTECTED_DATABASE_ROOTS,
    PROJECT_ROOT,
    database_path,
    load_config,
)
from .data_collection.gate import (
    DEFAULT_DEVELOPMENT_OUTPUT_PATH,
    DEFAULT_RELEASE_OUTPUT_PATH,
    run_gate as run_data_collection_gate,
)


V2_DATABASE_PATH = Path(r"D:\kinacesql\kinace_v2_1.sqlite3")
V2_EVALUATION_ROWS_PATH = (
    PROJECT_ROOT.parent / "kinace-v2" / "outputs" / "evaluation_rows.jsonl"
)
V3_DATA_ROOT = Path(r"D:\kinacesqlv3.2")
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "data_gate.json"

RAW_REPLAY_KEYS = (
    "raw_replay_inputs",
    "replay_inputs",
    "raw_features",
)


@dataclass(frozen=True)
class GateThresholds:
    min_decision_dates: int = 372
    min_evaluation_rows: int = 1
    min_nonzero_trades: int = 30


def build_read_only_uri(path: str | os.PathLike[str]) -> str:
    normalized = Path(path).resolve(strict=False).as_posix()
    return f"file:{quote(normalized, safe='/:')}?mode=ro&immutable=1"


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((str(path), str(root)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _json_mapping(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return _mapping(decoded)


def _has_raw_replay_marker(value: Mapping[str, Any] | None) -> bool:
    if value is None:
        return False
    for key in RAW_REPLAY_KEYS:
        marker = value.get(key)
        if isinstance(marker, Mapping) and bool(marker):
            return True
        if isinstance(marker, list) and bool(marker):
            return True
    return False


def _snapshot_has_raw_replay_inputs(payload_json: Any) -> bool:
    payload = _json_mapping(payload_json)
    if payload is None:
        return False
    if _has_raw_replay_marker(payload):
        return True
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    mappings = [_mapping(candidate) for candidate in candidates]
    return all(candidate is not None and _has_raw_replay_marker(candidate) for candidate in mappings)


def _item_has_raw_replay_inputs(feature_json: Any, recommendation_json: Any) -> bool:
    feature = _json_mapping(feature_json)
    recommendation = _json_mapping(recommendation_json)
    return _has_raw_replay_marker(feature) or _has_raw_replay_marker(recommendation)


def count_evaluation_rows(path: str | os.PathLike[str]) -> int:
    evaluation_path = Path(path)
    if not evaluation_path.is_file():
        return 0
    count = 0
    with evaluation_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                count += 1
    return count


def inspect_v2_database(
    path: str | os.PathLike[str] = V2_DATABASE_PATH,
) -> dict[str, Any]:
    database = Path(path).resolve(strict=False)
    uri = build_read_only_uri(database)
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        resolved = [
            row[2]
            for row in connection.execute("PRAGMA database_list").fetchall()
            if row[1] == "main"
        ]
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])

        snapshots = _table_count(connection, "recommendation_snapshots")
        recommendation_items = _table_count(connection, "recommendation_items")
        independent_dates = (
            int(
                connection.execute(
                    "SELECT COUNT(DISTINCT decision_date) FROM recommendation_snapshots"
                ).fetchone()[0]
            )
            if _table_exists(connection, "recommendation_snapshots")
            else 0
        )
        distinct_codes = (
            int(
                connection.execute(
                    "SELECT COUNT(DISTINCT code) FROM recommendation_items"
                ).fetchone()[0]
            )
            if _table_exists(connection, "recommendation_items")
            else 0
        )
        nonzero_trades = (
            int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM backtest_trades
                    WHERE shares IS NOT NULL AND shares <> 0
                    """
                ).fetchone()[0]
            )
            if _table_exists(connection, "backtest_trades")
            else 0
        )

        snapshot_raw_inputs = 0
        if _table_exists(connection, "recommendation_snapshots"):
            for row in connection.execute(
                "SELECT payload_json FROM recommendation_snapshots"
            ):
                snapshot_raw_inputs += int(_snapshot_has_raw_replay_inputs(row[0]))

        item_raw_inputs = 0
        if _table_exists(connection, "recommendation_items"):
            for row in connection.execute(
                "SELECT feature_json, recommendation_json FROM recommendation_items"
            ):
                item_raw_inputs += int(_item_has_raw_replay_inputs(row[0], row[1]))

        raw_replay_inputs_available = (
            snapshots > 0
            and recommendation_items > 0
            and snapshot_raw_inputs == snapshots
            and item_raw_inputs == recommendation_items
        )

        counts = {
            "independent_decision_dates": independent_dates,
            "snapshots": snapshots,
            "recommendation_items": recommendation_items,
            "distinct_codes": distinct_codes,
            "model_windows": _table_count(connection, "model_windows"),
            "candidates": _table_count(connection, "model_candidates"),
            "promotions": _table_count(connection, "model_promotions"),
            "backtest_runs": _table_count(connection, "backtest_runs"),
            "nonzero_trades": nonzero_trades,
        }
        return {
            "path": str(database),
            "uri": uri,
            "read_only": True,
            "immutable": True,
            "quick_check": quick_check,
            "resolved_path": str(resolved[0]) if resolved else None,
            "counts": counts,
            "raw_replay_inputs_available": raw_replay_inputs_available,
            "raw_replay_inputs": {
                "snapshots_with_inputs": snapshot_raw_inputs,
                "items_with_inputs": item_raw_inputs,
                "required_markers": list(RAW_REPLAY_KEYS),
            },
        }
    finally:
        connection.close()


def verify_v3_storage(
    root: str | os.PathLike[str] = V3_DATA_ROOT,
    v3_database: str | os.PathLike[str] = DEFAULT_DATABASE_PATH,
) -> dict[str, Any]:
    storage_root = Path(root).resolve(strict=False)
    database = Path(v3_database).resolve(strict=False)
    writable = False
    write_error: str | None = None

    try:
        storage_root.mkdir(parents=True, exist_ok=True)
        writable = storage_root.is_dir() and os.access(storage_root, os.W_OK)
    except OSError as exc:
        write_error = str(exc)

    outside_v2_tree = all(
        not _is_within(storage_root, root.resolve(strict=False))
        for root in PROTECTED_DATABASE_ROOTS
    )
    dedicated_database = _is_within(database, storage_root)
    v2_artifacts = sorted(
        str(path)
        for path in storage_root.glob("kinace_v2*.sqlite3*")
        if path.is_file()
    )

    return {
        "root": str(storage_root),
        "database": str(database),
        "writable": writable,
        "write_error": write_error,
        "outside_v2_tree": outside_v2_tree,
        "dedicated_database_path": dedicated_database,
        "v2_database_artifacts": v2_artifacts,
        "writable_only_by_v3": writable and outside_v2_tree and dedicated_database and not v2_artifacts,
    }


def _hard_gate(
    gate_id: str,
    description: str,
    actual: int | bool,
    required: int | bool,
    passed: bool,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "description": description,
        "actual": actual,
        "required": required,
        "passed": passed,
    }


def evaluate_data_gate(
    *,
    v2_database_path: str | os.PathLike[str] = V2_DATABASE_PATH,
    evaluation_rows_path: str | os.PathLike[str] = V2_EVALUATION_ROWS_PATH,
    v3_data_root: str | os.PathLike[str] = V3_DATA_ROOT,
    v3_database_path: str | os.PathLike[str] | None = None,
    thresholds: GateThresholds = GateThresholds(),
) -> dict[str, Any]:
    if v3_database_path is None:
        v3_database_path = database_path(load_config())

    database = inspect_v2_database(v2_database_path)
    counts = dict(database["counts"])
    counts["evaluation_rows"] = count_evaluation_rows(evaluation_rows_path)

    storage = verify_v3_storage(v3_data_root, v3_database_path)
    raw_replay_available = bool(database["raw_replay_inputs_available"])

    hard_gates = [
        _hard_gate(
            "v2_database_read_only_immutable",
            "V2.1 is opened read-only with SQLite immutable mode.",
            bool(database["read_only"] and database["immutable"]),
            True,
            bool(database["read_only"] and database["immutable"]),
        ),
        _hard_gate(
            "v3_storage_writable_and_isolated",
            "The V3 data root is writable and dedicated to V3.",
            bool(storage["writable_only_by_v3"]),
            True,
            bool(storage["writable_only_by_v3"]),
        ),
        _hard_gate(
            "independent_decision_dates",
            "Point-in-time history has enough independent decision dates.",
            counts["independent_decision_dates"],
            thresholds.min_decision_dates,
            counts["independent_decision_dates"] >= thresholds.min_decision_dates,
        ),
        _hard_gate(
            "labeled_evaluation_rows",
            "At least one labeled evaluation row is available.",
            counts["evaluation_rows"],
            thresholds.min_evaluation_rows,
            counts["evaluation_rows"] >= thresholds.min_evaluation_rows,
        ),
        _hard_gate(
            "raw_replay_inputs",
            "Every snapshot and recommendation item has raw replay inputs.",
            raw_replay_available,
            True,
            raw_replay_available,
        ),
        _hard_gate(
            "nonzero_backtest_trades",
            "The baseline evidence contains enough nonzero trades.",
            counts["nonzero_trades"],
            thresholds.min_nonzero_trades,
            counts["nonzero_trades"] >= thresholds.min_nonzero_trades,
        ),
    ]
    failed = [gate["id"] for gate in hard_gates if not gate["passed"]]
    ready = not failed

    data_gate_ids = {
        "independent_decision_dates",
        "labeled_evaluation_rows",
        "raw_replay_inputs",
        "nonzero_backtest_trades",
    }
    if any(gate_id in data_gate_ids for gate_id in failed):
        status = "blocked_insufficient_point_in_time_data"
    elif not storage["writable_only_by_v3"]:
        status = "blocked_v3_storage_isolation"
    else:
        status = "ready"

    return {
        "status": status,
        "ready": ready,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "database": database,
        "evaluation_rows_path": str(Path(evaluation_rows_path).resolve(strict=False)),
        "v3_storage": storage,
        "counts": counts,
        "raw_replay_inputs_available": raw_replay_available,
        "thresholds": {
            "min_decision_dates": thresholds.min_decision_dates,
            "min_evaluation_rows": thresholds.min_evaluation_rows,
            "min_nonzero_trades": thresholds.min_nonzero_trades,
        },
        "hard_gates": hard_gates,
        "failed_hard_gates": failed,
    }


def write_data_gate(
    result: Mapping[str, Any],
    output_path: str | os.PathLike[str] = DEFAULT_OUTPUT_PATH,
) -> Path:
    target = Path(output_path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)
    return target


def run_data_gate(
    output_path: str | os.PathLike[str] | None = None,
    *,
    mode: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if mode is not None:
        return run_data_collection_gate(
            mode=mode,
            output_path=output_path,
            **kwargs,
        )
    resolved_output = DEFAULT_OUTPUT_PATH if output_path is None else output_path
    result = evaluate_data_gate(**kwargs)
    result["output_path"] = str(Path(resolved_output).resolve(strict=False))
    write_data_gate(result, resolved_output)
    return result


__all__ = [
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_DEVELOPMENT_OUTPUT_PATH",
    "DEFAULT_RELEASE_OUTPUT_PATH",
    "GateThresholds",
    "V2_DATABASE_PATH",
    "V2_EVALUATION_ROWS_PATH",
    "V3_DATA_ROOT",
    "build_read_only_uri",
    "count_evaluation_rows",
    "evaluate_data_gate",
    "inspect_v2_database",
    "run_data_gate",
    "verify_v3_storage",
    "write_data_gate",
]
