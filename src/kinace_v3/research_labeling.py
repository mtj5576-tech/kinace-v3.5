"""Phase 2 multi-horizon labels and holding-period alignment."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import (
    canonical_json_bytes,
    load_feature_snapshot_artifact,
    sha256_bytes,
    sha256_file,
)
from .portfolio_backtest import discover_snapshots
from .price_basis import (
    ADJUSTED_PRICE_BASIS,
    RESEARCH_PRICE_BASES,
    PriceBasisError,
    normalize_price_basis,
)


SCHEMA_VERSION = 1
LABEL_HORIZONS = (1, 3, 5)
PRIMARY_LABEL_HORIZON = 5
DEFAULT_LABEL_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_labeling"
DEFAULT_LABEL_REPORT_DIR = PROJECT_ROOT / "reports" / "research_labeling"


class ResearchLabelingError(ValueError):
    """Raised when Phase 2 label alignment cannot be evaluated safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def label_field(horizon: int) -> str:
    if horizon == 1:
        return "forward_return"
    if horizon in LABEL_HORIZONS:
        return f"forward_return_{horizon}d"
    raise ResearchLabelingError(f"unsupported label horizon: {horizon}")


def normalize_label_horizon(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in LABEL_HORIZONS:
        raise ResearchLabelingError(
            "label horizon must be one of " + ", ".join(str(item) for item in LABEL_HORIZONS)
        )
    return value


def label_value(labels: Mapping[str, Any], horizon: int) -> float | None:
    horizon = normalize_label_horizon(horizon)
    value = labels.get(label_field(horizon))
    if value is None and horizon == 1:
        value = labels.get("forward_return_1d")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def build_forward_labels(
    rows: Sequence[Mapping[str, Any]],
    index: int,
    *,
    price_basis: str = ADJUSTED_PRICE_BASIS,
) -> dict[str, Any]:
    """Build y1/y3/y5 from next-open entry to each horizon close."""
    try:
        resolved_basis = normalize_price_basis(price_basis)
    except PriceBasisError as exc:
        raise ResearchLabelingError(str(exc)) from exc
    if resolved_basis not in RESEARCH_PRICE_BASES:
        raise ResearchLabelingError(
            "forward labels require adjusted or total_return price_basis"
        )
    if index < 0 or index + 1 >= len(rows):
        return {}
    entry = rows[index + 1]
    entry_price = float(entry.get("open") or 0.0)
    if entry_price <= 0:
        return {}
    entry_date = str(entry.get("trade_date") or "")
    horizons: dict[str, Any] = {}
    returns: dict[str, float | None] = {}
    for horizon in LABEL_HORIZONS:
        exit_index = index + horizon
        if exit_index >= len(rows):
            horizons[str(horizon)] = {
                "ready": False,
                "exit_date": None,
                "exit_price": None,
                "return": None,
            }
            returns[label_field(horizon)] = None
            continue
        exit_row = rows[exit_index]
        exit_price = float(exit_row.get("close") or 0.0)
        value = exit_price / entry_price - 1.0 if exit_price > 0 else None
        horizons[str(horizon)] = {
            "ready": value is not None,
            "exit_date": str(exit_row.get("trade_date") or ""),
            "exit_price": exit_price if exit_price > 0 else None,
            "return": value,
        }
        returns[label_field(horizon)] = value
    primary_field = label_field(PRIMARY_LABEL_HORIZON)
    return {
        **returns,
        "price_basis": resolved_basis,
        "forward_return_1d": returns.get("forward_return"),
        "primary_label": primary_field,
        "primary_return": returns.get(primary_field),
        "label_ready": returns.get(primary_field) is not None,
        "horizon_ready": {
            str(horizon): horizons[str(horizon)]["ready"] for horizon in LABEL_HORIZONS
        },
        "entry": {
            "date": entry_date,
            "price": entry_price,
            "timing": "next_trading_day_open",
        },
        "horizons": horizons,
    }


def summarize_holding_periods(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    holds = [float(trade.get("hold_sessions") or 0.0) for trade in trades]
    exit_reasons = Counter(str(trade.get("exit_reason") or "unknown") for trade in trades)
    atr_reasons = {"stop", "stop_gap", "target", "target_gap"}
    time_reasons = {"time_stop"}
    forced_reasons = {"end_of_backtest"}
    trade_count = len(trades)

    def rate(reasons: set[str]) -> float:
        return sum(exit_reasons.get(reason, 0) for reason in reasons) / trade_count if trade_count else 0.0

    return {
        "trade_count": trade_count,
        "average_hold_sessions": statistics.fmean(holds) if holds else None,
        "median_hold_sessions": statistics.median(holds) if holds else None,
        "min_hold_sessions": min(holds) if holds else None,
        "max_hold_sessions": max(holds) if holds else None,
        "atr_exit_count": sum(exit_reasons.get(reason, 0) for reason in atr_reasons),
        "atr_exit_rate": rate(atr_reasons),
        "time_exit_count": sum(exit_reasons.get(reason, 0) for reason in time_reasons),
        "time_exit_rate": rate(time_reasons),
        "forced_end_count": sum(exit_reasons.get(reason, 0) for reason in forced_reasons),
        "forced_end_rate": rate(forced_reasons),
        "exit_reason_counts": dict(sorted(exit_reasons.items())),
    }


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def _write_text(text: str, path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def _connect(database: str | Path) -> sqlite3.Connection:
    target = validate_database_path(database)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    return connection


def _loop_trades(loop_report_path: str | Path | None) -> list[Mapping[str, Any]]:
    if loop_report_path is None:
        return []
    report = json.loads(Path(loop_report_path).read_text(encoding="utf-8"))
    best = report.get("best_candidate") if isinstance(report, Mapping) else None
    best = best if isinstance(best, Mapping) else {}
    paths = [
        str(fold.get("portfolio_output_path") or "")
        for fold in best.get("folds") or []
        if isinstance(fold, Mapping)
    ]
    holdout = report.get("holdout") if isinstance(report, Mapping) else None
    holdout = holdout if isinstance(holdout, Mapping) else {}
    if holdout.get("portfolio_output_path"):
        paths.append(str(holdout["portfolio_output_path"]))
    trades: list[Mapping[str, Any]] = []
    for raw_path in paths:
        if not raw_path or not Path(raw_path).is_file():
            continue
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        trades.extend(
            item for item in payload.get("trades") or [] if isinstance(item, Mapping)
        )
    return trades


def _snapshot_label_coverage(snapshot_dir: str | Path) -> dict[str, Any]:
    snapshots = discover_snapshots(snapshot_dir)
    coverage = {str(horizon): 0 for horizon in LABEL_HORIZONS}
    total_rows = 0
    dates: list[str] = []
    for item in snapshots:
        payload = load_feature_snapshot_artifact(str(item["path"]))
        dates.append(str(payload.get("decision_date") or ""))
        for row in payload.get("rows") or []:
            if not isinstance(row, Mapping) or row.get("replay_ready") is not True:
                continue
            total_rows += 1
            labels = row.get("labels")
            labels = labels if isinstance(labels, Mapping) else {}
            for horizon in LABEL_HORIZONS:
                if label_value(labels, horizon) is not None:
                    coverage[str(horizon)] += 1
    return {
        "snapshot_count": len(snapshots),
        "date_range": {"start": min(dates) if dates else None, "end": max(dates) if dates else None},
        "replay_ready_rows": total_rows,
        "label_coverage": coverage,
        "label_coverage_ratio": {
            horizon: coverage[horizon] / total_rows if total_rows else 0.0
            for horizon in coverage
        },
    }


def initialize_labeling_schema(database: str | Path | None = None) -> Path:
    target = database_path() if database is None else validate_database_path(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_label_alignment_runs (
                alignment_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                primary_label_horizon INTEGER NOT NULL,
                snapshot_dir TEXT NOT NULL,
                snapshot_count INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                y5_coverage_ratio REAL NOT NULL,
                average_hold_sessions REAL,
                time_exit_rate REAL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                summary_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );
            """
        )
    return target


def _markdown_report(payload: Mapping[str, Any]) -> str:
    coverage = payload["label_coverage"]
    holding = payload["holding_periods"]
    return "\n".join(
        [
            "# Kinace Phase 2 Label and Holding Alignment",
            "",
            f"- Status: `{payload['status']}`",
            f"- Primary label horizon: `{payload['primary_label_horizon']}` sessions",
            f"- Snapshots: `{coverage['snapshot_count']}`",
            f"- Replay-ready rows: `{coverage['replay_ready_rows']}`",
            f"- y1 coverage: `{coverage['label_coverage_ratio']['1']}`",
            f"- y3 coverage: `{coverage['label_coverage_ratio']['3']}`",
            f"- y5 coverage: `{coverage['label_coverage_ratio']['5']}`",
            f"- Average holding sessions: `{holding['average_hold_sessions']}`",
            f"- ATR exit rate: `{holding['atr_exit_rate']}`",
            f"- Time exit rate: `{holding['time_exit_rate']}`",
            "",
        ]
    )


def run_label_alignment_audit(
    database: str | Path | None,
    *,
    snapshot_dir: str | Path,
    loop_report_path: str | Path | None = None,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_LABEL_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_LABEL_REPORT_DIR,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    coverage = _snapshot_label_coverage(snapshot_dir)
    holding = summarize_holding_periods(_loop_trades(loop_report_path))
    completed = bool(
        coverage["replay_ready_rows"] > 0
        and coverage["label_coverage_ratio"]["5"] > 0
    )
    identity = {
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "loop_report_path": str(Path(loop_report_path).resolve(strict=False))
        if loop_report_path
        else None,
        "primary_label_horizon": PRIMARY_LABEL_HORIZON,
        "training_mode": "rank_ic_primary_horizon",
        "training_label_field": label_field(PRIMARY_LABEL_HORIZON),
        "coverage": coverage,
        "holding_periods": holding,
    }
    alignment_id = sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False) / f"label-alignment-{alignment_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False) / f"label-alignment-{alignment_id}.md"
    )
    payload = {
        "artifact_type": "kinace_v3_phase2_label_alignment",
        "schema_version": SCHEMA_VERSION,
        "alignment_id": alignment_id,
        "status": "completed" if completed else "blocked_missing_primary_labels",
        "primary_label_horizon": PRIMARY_LABEL_HORIZON,
        "primary_label_field": label_field(PRIMARY_LABEL_HORIZON),
        "training_mode": "rank_ic_primary_horizon",
        "training_label_field": label_field(PRIMARY_LABEL_HORIZON),
        "invalidates_phase1b": True,
        "phase1b_rerun_required": True,
        "phase1b_invalidation_reason": "training label horizon changed from y1 to y5",
        "label_horizons": list(LABEL_HORIZONS),
        "entry_timing": "next_trading_day_open",
        "exit_timing": "close_at_horizon",
        "backtest_entry_timing": "next_trading_day_open",
        "backtest_execution_price_basis": "raw",
        "backtest_research_price_basis": "total_return",
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "loop_report_path": str(Path(loop_report_path).resolve(strict=False))
        if loop_report_path
        else None,
        "label_coverage": coverage,
        "holding_periods": holding,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    initialize_labeling_schema(target)
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO research_label_alignment_runs (
                alignment_id, status, primary_label_horizon, snapshot_dir,
                snapshot_count, row_count, y5_coverage_ratio,
                average_hold_sessions, time_exit_rate, artifact_path,
                artifact_sha256, summary_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alignment_id) DO UPDATE SET
                status=excluded.status,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                summary_json=excluded.summary_json,
                created_at_utc=excluded.created_at_utc
            """,
            (
                alignment_id,
                payload["status"],
                PRIMARY_LABEL_HORIZON,
                payload["snapshot_dir"],
                coverage["snapshot_count"],
                coverage["replay_ready_rows"],
                coverage["label_coverage_ratio"]["5"],
                holding["average_hold_sessions"],
                holding["time_exit_rate"],
                str(written),
                payload["artifact_sha256"],
                canonical_json_bytes(
                    {"status": payload["status"], "coverage": coverage, "holding": holding},
                    trailing_newline=False,
                ).decode("utf-8"),
                _utc_now(),
            ),
        )
    return payload


__all__ = [
    "DEFAULT_LABEL_OUTPUT_DIR",
    "DEFAULT_LABEL_REPORT_DIR",
    "LABEL_HORIZONS",
    "PRIMARY_LABEL_HORIZON",
    "ResearchLabelingError",
    "SCHEMA_VERSION",
    "build_forward_labels",
    "initialize_labeling_schema",
    "label_field",
    "label_value",
    "normalize_label_horizon",
    "run_label_alignment_audit",
    "summarize_holding_periods",
]
