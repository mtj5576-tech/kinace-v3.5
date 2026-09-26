"""Provisional Phase 1b decision from cumulative out-of-sample evidence."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .research_statistics import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_CONFIDENCE_LEVEL,
    DEFAULT_MIN_OBSERVATION_SESSIONS,
    DEFAULT_MIN_TRADE_COUNT,
    DEFAULT_RANDOM_SEED,
    DEFAULT_Z_CRITICAL,
    analyze_return_series,
    build_trial_ledger,
    initialize_statistics_schema,
)


SCHEMA_VERSION = 1
DEFAULT_DECISION_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_decisions"
DEFAULT_DECISION_REPORT_DIR = PROJECT_ROOT / "reports" / "research_decisions"


class ResearchDecisionError(ValueError):
    """Raised when a provisional Phase 1b decision cannot be produced safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDecisionError(f"{field} must be an object")
    return value


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchDecisionError(f"cannot load {field}: {exc}") from exc


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
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchDecisionError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchDecisionError(f"{field} must be finite")
    return result


def _equity_returns(equity: Any, field: str) -> tuple[list[float], list[str]]:
    if not isinstance(equity, list):
        raise ResearchDecisionError(f"{field} equity must be a list")
    points: list[tuple[str, float]] = []
    for item in equity:
        if not isinstance(item, Mapping):
            continue
        trade_date = str(item.get("trade_date") or "")
        value = item.get("total_assets")
        if value is None and item.get("cash") is not None:
            value = float(item.get("cash") or 0.0) + float(item.get("market_value") or 0.0)
        if trade_date and value is not None:
            points.append((trade_date, _finite_number(value, "total_assets")))
    points.sort(key=lambda item: item[0])
    if len(points) < 2:
        raise ResearchDecisionError(f"{field} equity curve is too short")
    values = [value for _, value in points]
    if any(value <= 0 for value in values):
        raise ResearchDecisionError(f"{field} equity values must be positive")
    return (
        [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))],
        [date for date, _ in points],
    )


def _metric_trade_count(metrics: Mapping[str, Any]) -> int:
    raw_closed_stats = metrics.get("closed_trade_stats")
    closed_stats = (
        raw_closed_stats if isinstance(raw_closed_stats, Mapping) else metrics
    )
    value = closed_stats.get("trade_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _protocol_sample_thresholds(loop_report: Mapping[str, Any]) -> tuple[int, int]:
    objective = loop_report.get("objective")
    constraints = (
        objective.get("constraints") if isinstance(objective, Mapping) else None
    )
    min_observation_sessions = DEFAULT_MIN_OBSERVATION_SESSIONS
    min_trade_count = DEFAULT_MIN_TRADE_COUNT
    if isinstance(constraints, Mapping):
        observation = constraints.get("min_observation_sessions")
        if isinstance(observation, Mapping):
            value = observation.get("threshold")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                min_observation_sessions = value
        trade = constraints.get("min_trade_count")
        if isinstance(trade, Mapping):
            value = trade.get("threshold")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                min_trade_count = value
    protocol = loop_report.get("evaluation_protocol")
    if isinstance(protocol, Mapping):
        fields = protocol.get("required_evaluation_fields")
        if isinstance(fields, Mapping):
            observation = fields.get("min_observation_sessions")
            trade = fields.get("min_trade_count")
            if isinstance(observation, int) and not isinstance(observation, bool):
                min_observation_sessions = observation
            if isinstance(trade, int) and not isinstance(trade, bool):
                min_trade_count = trade
    return min_observation_sessions, min_trade_count


def load_candidate_oos_returns(
    loop_report_path: str | Path,
    *,
    candidate_signature: str | None = None,
) -> dict[str, Any]:
    """Load and concatenate all walk-forward folds plus the final holdout."""
    loop_report = _mapping(
        _load_json(loop_report_path, "research loop report"), "research loop report"
    )
    loop_id = str(loop_report.get("loop_id") or "")
    best = _mapping(loop_report.get("best_candidate") or {}, "best_candidate")
    signature = str(best.get("signature") or "")
    if not loop_id or not signature:
        raise ResearchDecisionError("loop report is missing loop or candidate identity")
    if candidate_signature is not None and str(candidate_signature) != signature:
        raise ResearchDecisionError(
            "requested candidate signature does not match the loop report"
        )
    segments = [
        {
            "kind": "walk_forward_fold",
            "fold": int(fold.get("fold") or 0),
            "path": str(fold.get("portfolio_output_path") or ""),
            "expected_run_id": str(fold.get("portfolio_run_id") or ""),
            "expected_start": str(fold.get("validation_start") or ""),
            "expected_end": str(fold.get("validation_end") or ""),
        }
        for fold in best.get("folds") or []
        if isinstance(fold, Mapping)
    ]
    holdout = _mapping(loop_report.get("holdout") or {}, "holdout")
    segments.append(
        {
            "kind": "final_holdout",
            "fold": None,
            "path": str(holdout.get("portfolio_output_path") or ""),
            "expected_run_id": str(holdout.get("portfolio_run_id") or ""),
            "expected_start": str((holdout.get("date_range") or {}).get("start") or ""),
            "expected_end": str((holdout.get("date_range") or {}).get("end") or ""),
        }
    )
    if not segments or not segments[-1]["path"]:
        raise ResearchDecisionError("loop report has no holdout portfolio output")
    all_returns: list[float] = []
    resolved_segments: list[dict[str, Any]] = []
    total_trade_count = 0
    previous_end = ""
    for segment in segments:
        if not segment["path"]:
            raise ResearchDecisionError(f"{segment['kind']} portfolio output is missing")
        report = _mapping(
            _load_json(segment["path"], f"{segment['kind']} portfolio report"),
            "portfolio report",
        )
        run_id = str(report.get("run_id") or "")
        if segment["expected_run_id"] and run_id != segment["expected_run_id"]:
            raise ResearchDecisionError(
                f"{segment['kind']} run_id mismatch: {run_id}"
            )
        date_range = _mapping(report.get("date_range") or {}, "portfolio date_range")
        start = str(date_range.get("start") or "")
        end = str(date_range.get("end") or "")
        if segment["expected_start"] and start != segment["expected_start"]:
            raise ResearchDecisionError(f"{segment['kind']} start date mismatch")
        if segment["expected_end"] and end != segment["expected_end"]:
            raise ResearchDecisionError(f"{segment['kind']} end date mismatch")
        if previous_end and start <= previous_end:
            raise ResearchDecisionError("OOS portfolio windows overlap or are not ordered")
        returns, dates = _equity_returns(report.get("equity"), segment["kind"])
        all_returns.extend(returns)
        metrics = dict(report.get("metrics") or {})
        total_trade_count += _metric_trade_count(metrics)
        resolved_segments.append(
            {
                "kind": segment["kind"],
                "fold": segment["fold"],
                "run_id": run_id,
                "artifact_path": str(Path(segment["path"]).resolve(strict=False)),
                "date_range": {"start": start, "end": end},
                "sessions": len(returns),
                "first_date": dates[0],
                "last_date": dates[-1],
                "returns_hash": sha256_bytes(
                    canonical_json_bytes(returns, trailing_newline=False)
                ),
                "metrics": metrics,
            }
        )
        previous_end = end
    return {
        "loop_id": loop_id,
        "candidate_signature": signature,
        "candidate_model_config": dict(
            _mapping(best.get("model_config") or {}, "best_candidate.model_config")
        ),
        "candidate_backtest_config": dict(
            _mapping(best.get("backtest_config") or {}, "best_candidate.backtest_config")
        ),
        "min_observation_sessions": _protocol_sample_thresholds(loop_report)[0],
        "min_trade_count": _protocol_sample_thresholds(loop_report)[1],
        "segments": resolved_segments,
        "returns": all_returns,
        "trade_count": total_trade_count,
        "returns_hash": sha256_bytes(
            canonical_json_bytes(all_returns, trailing_newline=False)
        ),
    }


def initialize_decision_schema(database: str | Path | None = None) -> Path:
    target = initialize_statistics_schema(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_provisional_decisions (
                decision_id TEXT PRIMARY KEY,
                loop_id TEXT NOT NULL,
                candidate_signature TEXT NOT NULL,
                status TEXT NOT NULL,
                target_sharpe REAL NOT NULL,
                N_trials_total INTEGER NOT NULL,
                N_eff_measured REAL NOT NULL,
                N_eff_required REAL NOT NULL,
                sharpe_ci_lower REAL,
                dsr_p_value REAL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                summary_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_research_decisions_candidate
                ON research_provisional_decisions(candidate_signature, created_at_utc);
            """
        )
    return target


def _decision_status(checks: Mapping[str, bool]) -> str:
    return "provisional_passed" if all(checks.values()) else "provisional_rejected"


def run_provisional_decision(
    database: str | Path | None,
    loop_report_path: str | Path,
    *,
    target_sharpe: float,
    assume_recorded_trials_complete: bool,
    min_observation_sessions: int | None = None,
    min_trade_count: int | None = None,
    candidate_signature: str | None = None,
    unrecorded_trials_upper_bound: int | None = None,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    seed: int = DEFAULT_RANDOM_SEED,
    block_size: int | None = None,
    autocorrelation_truncation: int | None = None,
    z_critical: float = DEFAULT_Z_CRITICAL,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_DECISION_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_DECISION_REPORT_DIR,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    oos = load_candidate_oos_returns(
        loop_report_path,
        candidate_signature=candidate_signature,
    )
    ledger = build_trial_ledger(
        target,
        loop_id=oos["loop_id"],
        unrecorded_trials_upper_bound=unrecorded_trials_upper_bound,
        assume_recorded_trials_complete=assume_recorded_trials_complete,
    )
    if not ledger["ledger_accounting_complete"]:
        raise ResearchDecisionError(
            "trial ledger requires proven completeness or a frozen finite "
            "unrecorded upper bound for Phase 1b"
        )
    if int(ledger["N_trials_total"]) <= 0:
        raise ResearchDecisionError("trial ledger establishes no trials for Phase 1b")
    raw_min_sessions = (
        oos["min_observation_sessions"]
        if min_observation_sessions is None
        else min_observation_sessions
    )
    raw_min_trades = (
        oos["min_trade_count"] if min_trade_count is None else min_trade_count
    )
    if isinstance(raw_min_sessions, bool) or not isinstance(raw_min_sessions, int):
        raise ResearchDecisionError(
            "minimum observation sessions must be an integer for Phase 1b"
        )
    if isinstance(raw_min_trades, bool) or not isinstance(raw_min_trades, int):
        raise ResearchDecisionError(
            "minimum trade count must be an integer for Phase 1b"
        )
    resolved_min_sessions = raw_min_sessions
    resolved_min_trades = raw_min_trades
    if resolved_min_sessions <= 0:
        raise ResearchDecisionError(
            "minimum observation sessions must be established for Phase 1b"
        )
    if resolved_min_trades <= 0:
        raise ResearchDecisionError(
            "minimum trade count must be established for Phase 1b"
        )
    statistics_payload = analyze_return_series(
        oos["returns"],
        trade_count=int(oos["trade_count"]),
        min_observation_sessions=resolved_min_sessions,
        min_trade_count=resolved_min_trades,
        target_sharpe=target_sharpe,
        N_trials=int(ledger["N_trials_total"]),
        bootstrap_iterations=bootstrap_iterations,
        confidence_level=confidence_level,
        seed=seed,
        block_size=block_size,
        autocorrelation_truncation=autocorrelation_truncation,
        z_critical=z_critical,
    )
    sharpe_interval = statistics_payload["bootstrap_confidence_interval"].get(
        "annualized_sharpe"
    )
    raw_sharpe_lower = (
        sharpe_interval.get("lower") if isinstance(sharpe_interval, Mapping) else None
    )
    sharpe_lower = (
        float(raw_sharpe_lower)
        if isinstance(raw_sharpe_lower, (int, float))
        and not isinstance(raw_sharpe_lower, bool)
        and math.isfinite(float(raw_sharpe_lower))
        else None
    )
    dsr = statistics_payload["deflated_sharpe_ratio"]
    ci_computable = bool(
        statistics_payload["bootstrap_confidence_interval"].get("computable")
        is True
        and sharpe_lower is not None
    )
    dsr_computable = bool(dsr.get("computable") is True)
    raw_dsr_p_value = dsr.get("p_value")
    dsr_p_value = (
        float(raw_dsr_p_value)
        if dsr_computable
        and isinstance(raw_dsr_p_value, (int, float))
        and not isinstance(raw_dsr_p_value, bool)
        and math.isfinite(float(raw_dsr_p_value))
        else None
    )
    checks = {
        "trial_ledger_accounting_complete": bool(
            ledger["ledger_accounting_complete"]
        ),
        "minimum_observation_sessions_established": True,
        "minimum_observation_sessions_met": bool(
            statistics_payload["sample_requirements"]["sessions_met"]
        ),
        "minimum_trades_established": True,
        "minimum_trades_met": bool(
            statistics_payload["sample_requirements"]["trades_met"]
        ),
        "bootstrap_confidence_interval_computable": ci_computable,
        "cumulative_oos_sharpe_ci_lower_positive": bool(
            sharpe_lower is not None and sharpe_lower > 0
        ),
        "deflated_sharpe_computable": dsr_computable,
        "deflated_sharpe_p_value_below_0_05": bool(
            dsr_computable and dsr_p_value is not None and dsr_p_value < 0.05
        ),
        "effective_sample_size_meets_target": bool(
            statistics_payload["target_power"]["N_eff_met"] is True
        ),
    }
    status = _decision_status(checks)
    frozen_at = _utc_now()
    identity = {
        "schema_version": SCHEMA_VERSION,
        "loop_id": oos["loop_id"],
        "candidate_signature": oos["candidate_signature"],
        "returns_hash": oos["returns_hash"],
        "trade_count": int(oos["trade_count"]),
        "min_observation_sessions": resolved_min_sessions,
        "min_trade_count": resolved_min_trades,
        "ledger_hash": ledger["ledger_hash"],
        "target_sharpe": float(target_sharpe),
        "bootstrap_iterations": int(bootstrap_iterations),
        "confidence_level": float(confidence_level),
        "seed": int(seed),
        "block_size": block_size,
        "autocorrelation_truncation": autocorrelation_truncation,
        "z_critical": float(z_critical),
    }
    decision_id = sha256_bytes(
        canonical_json_bytes(identity, trailing_newline=False)
    )
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False)
        / f"provisional-decision-{decision_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False)
        / f"provisional-decision-{decision_id}.md"
    )
    payload = {
        "artifact_type": "kinace_v3_provisional_phase1b_decision",
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "status": status,
        "provisional": True,
        "invalidated_by_phase": 2,
        "must_rerun_after_phase": 2,
        "decision_scope": "current_candidate_before_label_and_holding_period_repair",
        "loop_id": oos["loop_id"],
        "candidate_signature": oos["candidate_signature"],
        "target_sharpe": float(target_sharpe),
        "target_sharpe_frozen": True,
        "target_sharpe_frozen_at_utc": frozen_at,
        "target_sharpe_source": "user_frozen_before_phase1b_execution",
        "trial_ledger": ledger,
        "oos_segments": oos["segments"],
        "oos_returns_hash": oos["returns_hash"],
        "checks": checks,
        "statistics": statistics_payload,
        "sample_requirements": statistics_payload["sample_requirements"],
        "allowed_next_action": (
            "proceed_to_phase2_then_rerun_phase1b"
            if status == "provisional_passed"
            else "continue_phase2_without_promotion"
        ),
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": frozen_at,
    }
    lines = [
        "# Kinace Phase 1b Provisional Decision",
        "",
        f"- Decision ID: `{decision_id}`",
        f"- Status: `{status}`",
        f"- Candidate: `{oos['candidate_signature']}`",
        f"- Target Sharpe: `{target_sharpe}`",
        f"- OOS sessions: `{statistics_payload['observed']['sessions']}`",
        f"- N_trials: `{ledger['N_trials_total']}`",
        f"- N_eff measured: `{statistics_payload['effective_sample_size']['N_eff_measured']}`",
        f"- N_eff required: `{statistics_payload['target_power']['N_eff_required']}`",
        f"- Sharpe CI lower: `{sharpe_lower}`",
        f"- DSR p-value: `{dsr.get('p_value')}`",
        "",
        "This decision is provisional and is invalidated by Phase 2.",
        "",
        "| Check | Passed |",
        "|---|---:|",
    ]
    lines.extend(f"| `{key}` | {value} |" for key, value in checks.items())
    lines.append("")
    _write_text("\n".join(lines), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    initialize_decision_schema(target)
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO research_provisional_decisions (
                decision_id, loop_id, candidate_signature, status, target_sharpe,
                N_trials_total, N_eff_measured, N_eff_required, sharpe_ci_lower,
                dsr_p_value, artifact_path, artifact_sha256, summary_json,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id) DO UPDATE SET
                status=excluded.status,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                summary_json=excluded.summary_json,
                created_at_utc=excluded.created_at_utc
            """,
            (
                decision_id,
                oos["loop_id"],
                oos["candidate_signature"],
                status,
                float(target_sharpe),
                int(ledger["N_trials_total"]),
                float(statistics_payload["effective_sample_size"]["N_eff_measured"]),
                float(statistics_payload["target_power"]["N_eff_required"]),
                sharpe_lower,
                (
                    float(dsr["p_value"])
                    if dsr.get("p_value") is not None
                    else None
                ),
                str(written),
                payload["artifact_sha256"],
                canonical_json_bytes(
                    {
                        "status": status,
                        "checks": checks,
                        "provisional": True,
                        "invalidated_by_phase": 2,
                    },
                    trailing_newline=False,
                ).decode("utf-8"),
                frozen_at,
            ),
        )
    return payload


__all__ = [
    "DEFAULT_DECISION_OUTPUT_DIR",
    "DEFAULT_DECISION_REPORT_DIR",
    "ResearchDecisionError",
    "SCHEMA_VERSION",
    "initialize_decision_schema",
    "load_candidate_oos_returns",
    "run_provisional_decision",
]
