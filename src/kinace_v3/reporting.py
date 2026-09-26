from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .config import PROJECT_ROOT, database_path, load_config, validate_database_path


DEFAULT_REPORT_PATH = PROJECT_ROOT / "outputs" / "reports" / "v3-report.md"
DEFAULT_DATA_GATE_PATH = PROJECT_ROOT / "outputs" / "data_gate.json"


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _read_only_uri(path: Path) -> str:
    normalized = path.resolve(strict=False).as_posix()
    return f"file:{quote(normalized, safe='/:')}?mode=ro"


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _count(connection: sqlite3.Connection, table: str, tables: set[str]) -> int:
    if table not in tables:
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _load_data_gate(path: str | Path = DEFAULT_DATA_GATE_PATH) -> dict[str, Any]:
    target = Path(path).resolve(strict=False)
    if not target.is_file():
        return {
            "status": "missing",
            "ready": False,
            "path": str(target),
            "failed_hard_gates": [],
        }
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "ready": False,
            "path": str(target),
            "failed_hard_gates": [],
            "error": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "ready": False,
            "path": str(target),
            "failed_hard_gates": [],
        }
    return {
        "status": payload.get("status", "missing"),
        "ready": payload.get("ready") is True,
        "path": str(target),
        "failed_hard_gates": list(payload.get("failed_hard_gates") or []),
        "generated_at_utc": payload.get("generated_at_utc"),
        "mode": payload.get("mode"),
        "development_only": payload.get("development_only") is True,
        "production_eligible": payload.get("production_eligible"),
    }


def build_report(
    *,
    database: str | Path | None = None,
    experiment_id: str | None = None,
    run_id: str | None = None,
    data_gate_path: str | Path = DEFAULT_DATA_GATE_PATH,
) -> dict[str, Any]:
    """Build a read-only research report; it never changes model state."""
    target = database_path(load_config()) if database is None else validate_database_path(database)
    gate = _load_data_gate(data_gate_path)
    base = {
        "artifact_type": "kinace_v3_research_report",
        "database": str(target),
        "data_gate": gate,
        "production_ready": False,
        "production_changed": False,
        "parity_claimed": False,
        "note": (
            "Research evidence only. The data gate may be release-ready, but "
            "production promotion remains dry-run only and V2.1 parity is not claimed."
        ),
    }
    if not target.is_file():
        return {
            **base,
            "status": "no_database",
            "counts": {},
            "runs": [],
        }

    connection = sqlite3.connect(_read_only_uri(target), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = _table_names(connection)
        counts = {
            "model_candidates": _count(connection, "model_candidates", tables),
            "score_replays": _count(connection, "score_replays", tables),
            "experiments": _count(connection, "experiments", tables),
            "feature_snapshots": _count(connection, "feature_snapshots", tables),
            "backtest_runs": _count(connection, "backtest_runs", tables),
            "backtest_trades": _count(connection, "backtest_trades", tables),
        }
        if "backtest_runs" not in tables or "experiments" not in tables:
            return {
                **base,
                "status": "no_runs",
                "counts": counts,
                "runs": [],
            }

        clauses: list[str] = []
        params: list[Any] = []
        if experiment_id:
            clauses.append("b.experiment_id=?")
            params.append(experiment_id)
        if run_id:
            clauses.append("b.run_id=?")
            params.append(run_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            f"""
            SELECT
                b.run_id, b.status, b.start_date, b.end_date,
                b.initial_cash, b.config_json, b.metrics_json,
                b.experiment_id, b.candidate_id, b.replay_id,
                e.code_version, e.data_snapshot_hash,
                e.search_space_json, e.objective_config_json, e.seed
            FROM backtest_runs b
            JOIN experiments e ON e.experiment_id=b.experiment_id
            {where}
            ORDER BY b.created_at_utc, b.run_id
            """,
            params,
        ).fetchall()
        runs = [
            {
                "run_id": row["run_id"],
                "status": row["status"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "initial_cash": row["initial_cash"],
                "config": _json_value(row["config_json"]),
                "metrics": _json_value(row["metrics_json"]),
                "experiment_id": row["experiment_id"],
                "candidate_id": row["candidate_id"],
                "replay_id": row["replay_id"],
                "code_version": row["code_version"],
                "data_snapshot_hash": row["data_snapshot_hash"],
                "search_space": _json_value(row["search_space_json"]),
                "objective_config": _json_value(row["objective_config_json"]),
                "seed": row["seed"],
            }
            for row in rows
        ]
        return {
            **base,
            "status": "available" if runs else "no_runs",
            "counts": counts,
            "runs": runs,
        }
    finally:
        connection.close()


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Kinace V3 Research Report",
        "",
        f"- Status: `{report.get('status', 'unknown')}`",
        f"- Database: `{report.get('database', '')}`",
        f"- Data gate: `{report.get('data_gate', {}).get('status', 'missing')}`",
        f"- Production ready: `false`",
        f"- Production changed: `false`",
        f"- V2.1 parity claimed: `false`",
        "",
        "## Inventory",
        "",
    ]
    for key, value in sorted((report.get("counts") or {}).items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Backtest Runs", ""])
    runs = report.get("runs") or []
    if not runs:
        lines.append("No candidate-specific backtest runs are recorded.")
    for run in runs:
        metrics = run.get("metrics") or {}
        lines.extend(
            [
                f"### {run.get('run_id')}",
                "",
                f"- Candidate: `{run.get('candidate_id')}`",
                f"- Replay: `{run.get('replay_id')}`",
                f"- Experiment: `{run.get('experiment_id')}`",
                f"- Date range: `{run.get('start_date')}` to `{run.get('end_date')}`",
                f"- Final equity: `{metrics.get('final_equity')}`",
                f"- Maximum drawdown: `{metrics.get('max_drawdown')}`",
                f"- Trade count: `{metrics.get('trade_count')}`",
                "",
            ]
        )
    lines.extend(["## Boundary", "", str(report.get("note") or ""), ""])
    return "\n".join(lines)


def write_report(
    report: Mapping[str, Any],
    output_path: str | Path = DEFAULT_REPORT_PATH,
) -> Path:
    target = Path(output_path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


__all__ = [
    "DEFAULT_REPORT_PATH",
    "build_report",
    "render_markdown",
    "write_report",
]
