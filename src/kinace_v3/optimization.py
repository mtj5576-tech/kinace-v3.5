"""Backtest-driven composite objective and deterministic parameter optimization."""

from __future__ import annotations

import json
import math
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .history_features import DEFAULT_MODEL_CONFIG
from .portfolio_backtest import (
    PortfolioBacktestError,
    load_market_bars,
    run_portfolio_backtest,
    validate_snapshot_directory_for_research,
)
from .score_replay import candidate_id_for, validate_candidate_config
from .search import candidate_configs_from_payload
from .trial_backtest import (
    MARK_TO_MARKET_EQUITY_BASIS,
    initialize_trial_backtest_schema,
    normalize_backtest_config,
)


SCHEMA_VERSION = 1
MAX_OPTIMIZATION_CANDIDATES = 64
DEFAULT_OPTIMIZATION_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "optimization"
DEFAULT_OPTIMIZATION_REPORT_DIR = PROJECT_ROOT / "reports" / "optimization"
OBJECTIVE_SCHEMA_VERSION = 2
DEFAULT_OBJECTIVE: dict[str, Any] = {
    "schema_version": OBJECTIVE_SCHEMA_VERSION,
    "metric": "kinace_v3_primary_net_sharpe",
    "policy": {
        "version": "kinace-v3.3-phase3-objective-policy-v1",
        "source": "phase3_risk_policy",
        "selected_on": "development_walk_forward_only",
        "holdout_used_for_selection": False,
        "frozen_before_holdout": True,
        "objective_weight_source": "risk_policy_fixed",
        "migrated_from": None,
    },
    "primary": {
        "metric": "net_sharpe",
        "source_metric": "sharpe",
        "equity_basis": MARK_TO_MARKET_EQUITY_BASIS,
        "direction": "maximize",
        "sign": 1.0,
        "normalization": "identity",
        "clip_min": -5.0,
        "clip_max": 5.0,
        "weight": 1.0,
        "source": "phase3_risk_policy",
    },
    "constraints": {
        "min_observation_sessions": {
            "operator": ">=",
            "threshold": 60,
            "binding": True,
            "source": "phase3_risk_policy",
        },
        "min_trade_count": {
            "operator": ">=",
            "threshold": 30,
            "binding": True,
            "source": "phase3_risk_policy",
        },
        "max_drawdown": {
            "operator": "<=",
            "threshold": 0.60,
            "binding": True,
            "source": "phase3_risk_policy",
        },
        "max_turnover_per_session": {
            "operator": "<=",
            "threshold": 0.50,
            "binding": True,
            "source": "phase3_risk_policy",
        },
        "min_profit_factor": {
            "operator": ">=",
            "threshold": 1.0,
            "binding": True,
            "source": "phase3_risk_policy",
        },
        "min_total_return": {
            "operator": ">=",
            "threshold": 0.0,
            "binding": True,
            "source": "phase3_risk_policy",
        },
        "min_win_rate": {
            "operator": ">=",
            "threshold": 0.0,
            "binding": True,
            "source": "phase3_risk_policy",
        },
    },
    "report_only": {
        "max_feature_weight": {
            "operator": "<=",
            "threshold": 0.75,
            "binding": False,
            "source": "phase4_transition",
        }
    },
}
LEGACY_OBJECTIVE_KEYS = frozenset({"weights", "hard_gates"})
OBJECTIVE_KEYS = frozenset(
    {"schema_version", "metric", "policy", "primary", "constraints", "report_only"}
)
PRIMARY_METRIC = "net_sharpe"
PRIMARY_SOURCE_METRIC = "sharpe"
CONSTRAINT_OPERATORS = {
    "min_observation_sessions": ">=",
    "min_trade_count": ">=",
    "max_drawdown": "<=",
    "max_turnover_per_session": "<=",
    "min_profit_factor": ">=",
    "min_total_return": ">=",
    "min_win_rate": ">=",
}


class OptimizationError(ValueError):
    """Raised when backtest-driven parameter optimization is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OptimizationError(f"{field} must be an object")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OptimizationError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise OptimizationError(f"{field} must be a finite number")
    return number


def _optional_finite_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, field)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OptimizationError(f"{field} must be a non-negative integer")
    return value


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def normalize_objective_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = dict(config or {})
    unknown = sorted(set(source) - OBJECTIVE_KEYS - LEGACY_OBJECTIVE_KEYS)
    if unknown:
        raise OptimizationError("unknown objective keys: " + ", ".join(unknown))
    result = deepcopy(DEFAULT_OBJECTIVE)
    legacy = bool(set(source) & LEGACY_OBJECTIVE_KEYS)
    new_shape = bool(set(source) & (OBJECTIVE_KEYS - {"metric"}))
    if legacy and new_shape:
        raise OptimizationError(
            "legacy objective weights/hard_gates cannot be mixed with primary/constraints"
        )
    if legacy:
        result["policy"]["source"] = "legacy_objective_migration"
        result["policy"]["migrated_from"] = "kinace_v3_composite_backtest_objective"
        gates = source.get("hard_gates")
        if gates is not None:
            gates = _mapping(gates, "objective.hard_gates")
            mappings = {
                "min_observation_sessions": "min_observation_sessions",
                "min_trade_count": "min_trade_count",
                "max_drawdown": "max_drawdown",
                "min_profit_factor": "min_profit_factor",
                "min_total_return": "min_total_return",
                "min_win_rate": "min_win_rate",
                "max_turnover_per_session": "max_turnover_per_session",
            }
            unknown_gates = sorted(set(gates) - set(mappings))
            if unknown_gates:
                raise OptimizationError(
                    "unknown objective hard gates: " + ", ".join(unknown_gates)
                )
            for source_key, target_key in mappings.items():
                if source_key in gates:
                    result["constraints"][target_key]["threshold"] = gates[source_key]
            if "min_observation_sessions" not in gates:
                # Legacy objectives did not pre-register a session minimum. Keep
                # their behavior explicit rather than retroactively changing it.
                result["constraints"]["min_observation_sessions"]["threshold"] = 0
    else:
        result["metric"] = str(source.get("metric") or result["metric"])
        if "schema_version" in source:
            version = source["schema_version"]
            if isinstance(version, bool) or not isinstance(version, int):
                raise OptimizationError("objective.schema_version must be an integer")
            if version != OBJECTIVE_SCHEMA_VERSION:
                raise OptimizationError(
                    f"objective.schema_version must be {OBJECTIVE_SCHEMA_VERSION}"
                )
        for key in ("policy", "primary", "constraints", "report_only"):
            if key not in source:
                continue
            values = _mapping(source[key], f"objective.{key}")
            unknown_values = sorted(set(values) - set(result[key]))
            if unknown_values:
                raise OptimizationError(
                    f"unknown objective.{key} keys: " + ", ".join(unknown_values)
                )
            if key == "constraints":
                for name, value in values.items():
                    result[key][name].update(_mapping(value, f"objective.{key}.{name}"))
            else:
                result[key].update(values)

    if result["metric"] != "kinace_v3_primary_net_sharpe":
        raise OptimizationError("objective.metric must be kinace_v3_primary_net_sharpe")
    policy = result["policy"]
    if policy.get("holdout_used_for_selection") is not False:
        raise OptimizationError(
            "objective.policy.holdout_used_for_selection must be false"
        )
    if policy.get("frozen_before_holdout") is not True:
        raise OptimizationError("objective.policy.frozen_before_holdout must be true")
    primary = result["primary"]
    if primary["metric"] != PRIMARY_METRIC:
        raise OptimizationError("objective.primary.metric must be net_sharpe")
    if primary["source_metric"] != PRIMARY_SOURCE_METRIC:
        raise OptimizationError("objective.primary.source_metric must be sharpe")
    if primary["equity_basis"] != MARK_TO_MARKET_EQUITY_BASIS:
        raise OptimizationError(
            "objective.primary.equity_basis must be mark_to_market"
        )
    if primary["direction"] != "maximize":
        raise OptimizationError("objective.primary.direction must be maximize")
    primary["sign"] = _finite_number(primary["sign"], "objective.primary.sign")
    if primary["sign"] != 1.0:
        raise OptimizationError("objective.primary.sign must be 1.0")
    if primary["normalization"] != "identity":
        raise OptimizationError("objective.primary.normalization must be identity")
    primary["clip_min"] = _finite_number(
        primary["clip_min"], "objective.primary.clip_min"
    )
    primary["clip_max"] = _finite_number(
        primary["clip_max"], "objective.primary.clip_max"
    )
    if primary["clip_min"] > primary["clip_max"]:
        raise OptimizationError("objective.primary clip range is invalid")
    primary["weight"] = _finite_number(
        primary["weight"], "objective.primary.weight"
    )
    if primary["weight"] != 1.0:
        raise OptimizationError("objective.primary.weight must be 1.0")
    for name, operator in CONSTRAINT_OPERATORS.items():
        constraint = result["constraints"][name]
        if constraint["operator"] != operator:
            raise OptimizationError(
                f"objective.constraints.{name}.operator must be {operator}"
            )
        if constraint["binding"] is not True:
            raise OptimizationError(
                f"objective.constraints.{name}.binding must be true"
            )
        if name in {"min_observation_sessions", "min_trade_count"}:
            constraint["threshold"] = _nonnegative_int(
                constraint["threshold"], f"objective.constraints.{name}.threshold"
            )
        else:
            constraint["threshold"] = _finite_number(
                constraint["threshold"], f"objective.constraints.{name}.threshold"
            )
    if not 0 <= float(result["constraints"]["max_drawdown"]["threshold"]) <= 1:
        raise OptimizationError(
            "objective.constraints.max_drawdown.threshold must be in [0, 1]"
        )
    if float(result["constraints"]["max_turnover_per_session"]["threshold"]) < 0:
        raise OptimizationError(
            "objective.constraints.max_turnover_per_session.threshold must be non-negative"
        )
    if not 0 <= float(result["constraints"]["min_win_rate"]["threshold"]) <= 1:
        raise OptimizationError(
            "objective.constraints.min_win_rate.threshold must be in [0, 1]"
        )
    report_only = result["report_only"]["max_feature_weight"]
    if report_only["operator"] != "<=":
        raise OptimizationError(
            "objective.report_only.max_feature_weight.operator must be <="
        )
    if report_only["binding"] is not False:
        raise OptimizationError(
            "objective.report_only.max_feature_weight.binding must be false in Phase 3"
        )
    report_only["threshold"] = _finite_number(
        report_only["threshold"],
        "objective.report_only.max_feature_weight.threshold",
    )
    if not 0 <= report_only["threshold"] <= 1:
        raise OptimizationError(
            "objective.report_only.max_feature_weight.threshold must be in [0, 1]"
        )
    return result


def score_portfolio_objective(
    metrics: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one continuous portfolio result and apply explicit hard gates."""
    objective = normalize_objective_config(config)
    values = _mapping(metrics, "metrics")
    if values.get("equity_basis") != MARK_TO_MARKET_EQUITY_BASIS:
        raise OptimizationError(
            "metrics.equity_basis must be mark_to_market"
        )
    raw_closed_stats = values.get("closed_trade_stats")
    closed_stats = (
        _mapping(raw_closed_stats, "metrics.closed_trade_stats")
        if isinstance(raw_closed_stats, Mapping)
        else values
    )
    trade_count = _nonnegative_int(
        int(closed_stats.get("trade_count") or 0), "metrics.trade_count"
    )
    total_return = _finite_number(values.get("total_return"), "metrics.total_return")
    max_drawdown = _finite_number(values.get("max_drawdown"), "metrics.max_drawdown")
    win_rate = _finite_number(closed_stats.get("win_rate"), "metrics.win_rate")
    turnover = _finite_number(values.get("turnover"), "metrics.turnover")
    annualized_return = _optional_finite_number(
        values.get("annualized_return"), "metrics.annualized_return"
    )
    excess_return = _optional_finite_number(
        values.get("excess_return"), "metrics.excess_return"
    )
    sharpe = _optional_finite_number(values.get("sharpe"), "metrics.sharpe")
    sortino = _optional_finite_number(values.get("sortino"), "metrics.sortino")
    raw_profit_factor = closed_stats.get("profit_factor")
    if raw_profit_factor is None:
        profit_factor = 3.0 if total_return > 0 and trade_count > 0 else 0.0
    else:
        profit_factor = _finite_number(raw_profit_factor, "metrics.profit_factor")

    trading_days = _nonnegative_int(
        int(values.get("trading_days") or 0), "metrics.trading_days"
    )
    turnover_per_session = turnover / trading_days if trading_days > 0 else turnover
    constraint_values = {
        "min_observation_sessions": trading_days,
        "min_trade_count": trade_count,
        "max_drawdown": max_drawdown,
        "max_turnover_per_session": turnover_per_session,
        "min_profit_factor": profit_factor,
        "min_total_return": total_return,
        "min_win_rate": win_rate,
    }
    constraints: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for name, actual in constraint_values.items():
        constraint = objective["constraints"][name]
        threshold = constraint["threshold"]
        passed = (
            actual >= float(threshold)
            if constraint["operator"] == ">="
            else actual <= float(threshold)
        )
        constraints[name] = {
            "actual": round(float(actual), 8),
            "operator": constraint["operator"],
            "threshold": threshold,
            "binding": True,
            "source": constraint["source"],
            "passed": bool(passed),
        }
        if not passed:
            failures.append(name)

    sample_unavailable_reasons: list[str] = []
    if trading_days < int(
        objective["constraints"]["min_observation_sessions"]["threshold"]
    ):
        sample_unavailable_reasons.append("minimum_observation_sessions_not_met")
    if trade_count < int(objective["constraints"]["min_trade_count"]["threshold"]):
        sample_unavailable_reasons.append("minimum_trade_count_not_met")
    sample_requirements = {
        "passed": not sample_unavailable_reasons,
        "available_sessions": trading_days,
        "required_sessions": int(
            objective["constraints"]["min_observation_sessions"]["threshold"]
        ),
        "available_trades": trade_count,
        "required_trades": int(
            objective["constraints"]["min_trade_count"]["threshold"]
        ),
        "unavailable_reasons": sample_unavailable_reasons,
    }

    primary = objective["primary"]
    if sample_unavailable_reasons:
        annualized_return = None
        excess_return = None
        sharpe = None
        sortino = None
        primary_raw = None
        primary_score = None
        unavailable_reasons = sample_unavailable_reasons
        status = "unavailable"
    elif sharpe is None:
        primary_raw = None
        primary_score = None
        unavailable_reasons = ["sharpe_unavailable"]
        status = "unavailable"
    else:
        primary_raw = sharpe
        primary_score = float(primary["sign"]) * min(
            float(primary["clip_max"]),
            max(float(primary["clip_min"]), primary_raw),
        )
        unavailable_reasons = []
        status = "passed" if not failures else "rejected"

    report_only = deepcopy(objective["report_only"])
    raw_max_feature_weight = values.get("max_feature_weight")
    if (
        isinstance(raw_max_feature_weight, bool)
        or not isinstance(raw_max_feature_weight, (int, float))
    ):
        report_only["max_feature_weight"]["actual"] = None
        report_only["max_feature_weight"]["passed"] = None
        report_only["max_feature_weight"]["evaluation_status"] = "not_evaluated"
    else:
        actual_max_feature_weight = float(raw_max_feature_weight)
        report_only["max_feature_weight"]["actual"] = round(
            actual_max_feature_weight, 8
        )
        report_only["max_feature_weight"]["passed"] = (
            actual_max_feature_weight
            <= float(report_only["max_feature_weight"]["threshold"])
        )
        report_only["max_feature_weight"]["evaluation_status"] = "reported"
    components = (
        {PRIMARY_METRIC: round(primary_score, 8)}
        if primary_score is not None
        else {}
    )
    return {
        "status": status,
        "score": round(primary_score, 8) if primary_score is not None else None,
        "primary_metric": PRIMARY_METRIC,
        "primary_source_metric": PRIMARY_SOURCE_METRIC,
        "primary_equity_basis": MARK_TO_MARKET_EQUITY_BASIS,
        "primary_raw": round(primary_raw, 8) if primary_raw is not None else None,
        "components": components,
        "constraints": constraints,
        "report_only": report_only,
        "hard_gate_failures": failures,
        "sample_requirements": sample_requirements,
        "unavailable_reason": ";".join(unavailable_reasons) if unavailable_reasons else None,
        "date_range": {
            "start": values.get("start_date"),
            "end": values.get("end_date"),
        },
        "normalized": {
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "profit_factor": profit_factor,
            "win_rate": win_rate,
            "turnover": turnover,
            "trading_days": trading_days,
            "turnover_per_session": round(turnover_per_session, 8),
            "trade_count": trade_count,
            "annualized_return": annualized_return,
            "excess_return": excess_return,
            "sharpe": sharpe,
            "sortino": sortino,
            "equity_basis": MARK_TO_MARKET_EQUITY_BASIS,
        },
    }


def _candidate_grid(
    payload: Mapping[str, Any],
    *,
    base_key: str,
    space_key: str,
    explicit_key: str,
    default_base: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if explicit_key in payload:
        values = candidate_configs_from_payload(payload[explicit_key])
    else:
        base = payload.get(base_key, default_base)
        if not isinstance(base, Mapping):
            raise OptimizationError(f"{base_key} must be an object")
        space = payload.get(space_key, {})
        values = candidate_configs_from_payload(
            {"base_config": dict(base), "search_space": dict(_mapping(space, space_key))}
        )
    return [dict(item) for item in values]


def normalize_optimization_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expand model and execution grids into a deterministic candidate matrix."""
    source = dict(_mapping(payload, "optimization payload"))
    objective = normalize_objective_config(
        dict(source.get("objective") or {}) if "objective" in source else None
    )
    raw_candidates = source.get("candidates")
    if isinstance(raw_candidates, list) and raw_candidates:
        if all(
            isinstance(item, Mapping) and "model_config" in item
            for item in raw_candidates
        ):
            if len(raw_candidates) > MAX_OPTIMIZATION_CANDIDATES:
                raise OptimizationError(
                    f"optimization matrix exceeds {MAX_OPTIMIZATION_CANDIDATES} candidates"
                )
            candidates = [
                {
                    "model_config": validate_candidate_config(
                        _mapping(item.get("model_config"), f"candidates[{index}].model_config")
                    ),
                    "backtest_config": normalize_backtest_config(
                        dict(item.get("backtest_config") or {})
                    ),
                }
                for index, item in enumerate(raw_candidates)
            ]
            return {"candidates": candidates, "objective": objective}
    if "model_candidates" not in source and "candidates" in source:
        source["model_candidates"] = source["candidates"]
    model_candidates = _candidate_grid(
        source,
        base_key="base_model_config",
        space_key="model_search_space",
        explicit_key="model_candidates",
        default_base=DEFAULT_MODEL_CONFIG,
    )
    backtest_candidates = _candidate_grid(
        source,
        base_key="base_backtest_config",
        space_key="backtest_search_space",
        explicit_key="backtest_candidates",
        default_base={},
    )
    candidate_count = len(model_candidates) * len(backtest_candidates)
    if candidate_count > MAX_OPTIMIZATION_CANDIDATES:
        raise OptimizationError(
            f"optimization matrix exceeds {MAX_OPTIMIZATION_CANDIDATES} candidates"
        )
    candidates = [
        {
            "model_config": validate_candidate_config(model_config),
            "backtest_config": normalize_backtest_config(backtest_config),
        }
        for model_config, backtest_config in product(model_candidates, backtest_candidates)
    ]
    return {
        "candidates": candidates,
        "objective": objective,
    }


def _connect(database: str | Path) -> sqlite3.Connection:
    target = validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_optimization_schema(database: str | Path | None = None) -> Path:
    target = initialize_trial_backtest_schema(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS optimization_runs (
                optimization_id TEXT PRIMARY KEY,
                code_version TEXT NOT NULL,
                status TEXT NOT NULL,
                snapshot_set_hash TEXT,
                candidate_count INTEGER NOT NULL,
                best_candidate_id TEXT,
                best_portfolio_run_id TEXT,
                objective_json TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS optimization_candidates (
                optimization_id TEXT NOT NULL REFERENCES optimization_runs(optimization_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                candidate_id TEXT NOT NULL,
                portfolio_run_id TEXT,
                status TEXT NOT NULL,
                objective_score REAL,
                hard_gate_failures_json TEXT NOT NULL,
                components_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                model_config_json TEXT NOT NULL,
                backtest_config_json TEXT NOT NULL,
                portfolio_output_path TEXT,
                PRIMARY KEY (optimization_id, sequence)
            );

            CREATE INDEX IF NOT EXISTS idx_optimization_candidates_run
                ON optimization_candidates(portfolio_run_id, candidate_id);
            """
        )
    return target


def _optimization_id_for(
    *,
    code_version: str,
    candidates: Sequence[Mapping[str, Any]],
    objective: Mapping[str, Any],
    snapshot_dir: str | Path,
    start_date: str | None,
    end_date: str | None,
) -> str:
    identity = {
        "code_version": code_version,
        "candidates": [deepcopy(dict(item)) for item in candidates],
        "objective": deepcopy(dict(objective)),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "start_date": start_date,
        "end_date": end_date,
    }
    return sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))


def _persist_optimization(
    database: str | Path,
    *,
    optimization_id: str,
    code_version: str,
    status: str,
    snapshot_set_hash: str | None,
    candidate_count: int,
    best_candidate_id: str | None,
    best_portfolio_run_id: str | None,
    objective: Mapping[str, Any],
    artifact_path: str | Path,
    artifact_sha256: str | None,
    candidates: Sequence[Mapping[str, Any]],
) -> None:
    initialize_optimization_schema(database)
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO optimization_runs (
                optimization_id, code_version, status, snapshot_set_hash,
                candidate_count, best_candidate_id, best_portfolio_run_id,
                objective_json, artifact_path, artifact_sha256, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(optimization_id) DO UPDATE SET
                code_version=excluded.code_version,
                status=excluded.status,
                snapshot_set_hash=excluded.snapshot_set_hash,
                candidate_count=excluded.candidate_count,
                best_candidate_id=excluded.best_candidate_id,
                best_portfolio_run_id=excluded.best_portfolio_run_id,
                objective_json=excluded.objective_json,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256
            """,
            (
                optimization_id,
                code_version,
                status,
                snapshot_set_hash,
                int(candidate_count),
                best_candidate_id,
                best_portfolio_run_id,
                canonical_json_bytes(objective, trailing_newline=False).decode("utf-8"),
                str(Path(artifact_path).resolve(strict=False)),
                artifact_sha256,
                _utc_now(),
            ),
        )
        connection.execute(
            "DELETE FROM optimization_candidates WHERE optimization_id=?",
            (optimization_id,),
        )
        for sequence, candidate in enumerate(candidates):
            connection.execute(
                """
                INSERT INTO optimization_candidates (
                    optimization_id, sequence, candidate_id, portfolio_run_id,
                    status, objective_score, hard_gate_failures_json,
                    components_json, metrics_json, model_config_json,
                    backtest_config_json, portfolio_output_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    optimization_id,
                    sequence,
                    str(candidate["candidate_id"]),
                    candidate.get("portfolio_run_id"),
                    str(candidate["status"]),
                    candidate.get("objective_score"),
                    canonical_json_bytes(
                        candidate.get("hard_gate_failures") or [],
                        trailing_newline=False,
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        candidate.get("components") or {},
                        trailing_newline=False,
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        candidate.get("metrics") or {},
                        trailing_newline=False,
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        candidate["model_config"], trailing_newline=False
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        candidate["backtest_config"], trailing_newline=False
                    ).decode("utf-8"),
                    candidate.get("portfolio_output_path"),
                ),
            )


def run_parameter_optimization(
    database: str | Path | None,
    payload: Mapping[str, Any],
    *,
    snapshot_dir: str | Path,
    start_date: str | None = None,
    end_date: str | None = None,
    bars: Any | None = None,
    output_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OPTIMIZATION_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_OPTIMIZATION_REPORT_DIR,
    code_version: str = "kinace-v3",
    portfolio_runner: Callable[..., Mapping[str, Any]] = run_portfolio_backtest,
) -> dict[str, Any]:
    """Evaluate a deterministic candidate matrix with continuous portfolio evidence."""
    target = database_path() if database is None else validate_database_path(database)
    try:
        validate_snapshot_directory_for_research(snapshot_dir)
    except PortfolioBacktestError as exc:
        raise OptimizationError(str(exc)) from exc
    normalized = normalize_optimization_payload(payload)
    candidates = normalized["candidates"]
    objective = normalized["objective"]
    optimization_id = _optimization_id_for(
        code_version=code_version,
        candidates=candidates,
        objective=objective,
        snapshot_dir=snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    resolved_output_dir = Path(output_dir).resolve(strict=False) / optimization_id
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(report_dir).resolve(strict=False) / f"optimization-{optimization_id}.json"
    )
    shared_bars = bars if bars is not None else load_market_bars(target)
    results: list[dict[str, Any]] = []
    snapshot_set_hash: str | None = None

    for index, candidate in enumerate(candidates):
        model_config = candidate["model_config"]
        backtest_config = candidate["backtest_config"]
        candidate_id = candidate_id_for(model_config)
        candidate_output = resolved_output_dir / f"candidate-{index:04d}.json"
        try:
            portfolio = portfolio_runner(
                target,
                model_config,
                snapshot_dir,
                start_date=start_date,
                end_date=end_date,
                bars=shared_bars,
                config=backtest_config,
                output_path=candidate_output,
                code_version=code_version,
            )
            scored = score_portfolio_objective(portfolio["metrics"], objective)
            if snapshot_set_hash is None:
                snapshot_set_hash = str(
                    (portfolio.get("snapshot_manifest") or {}).get("artifact_sha256") or ""
                ) or None
            results.append(
                {
                    "sequence": index,
                    "candidate_id": candidate_id,
                    "portfolio_run_id": portfolio.get("run_id"),
                    "portfolio_output_path": portfolio.get("output_path"),
                    "status": scored["status"],
                    "objective_score": scored["score"],
                    "winner_eligible": scored["status"] == "passed",
                    "components": scored["components"],
                    "hard_gate_failures": scored["hard_gate_failures"],
                    "normalized": scored["normalized"],
                    "metrics": dict(portfolio["metrics"]),
                    "model_config": model_config,
                    "backtest_config": backtest_config,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "sequence": index,
                    "candidate_id": candidate_id,
                    "portfolio_run_id": None,
                    "portfolio_output_path": str(candidate_output),
                    "status": "failed",
                    "objective_score": None,
                    "winner_eligible": False,
                    "components": {},
                    "hard_gate_failures": ["runtime_error"],
                    "normalized": {},
                    "metrics": {},
                    "model_config": model_config,
                    "backtest_config": backtest_config,
                    "error": str(exc),
                }
            )

    ranked = sorted(
        results,
        key=lambda item: (
            item["status"] == "passed",
            item.get("objective_score") is not None,
            item.get("objective_score") if item.get("objective_score") is not None else float("-inf"),
            -int(item["sequence"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    best = next((item for item in ranked if item["status"] == "passed"), None)
    report = {
        "artifact_type": "kinace_v3_backtest_driven_optimization",
        "schema_version": SCHEMA_VERSION,
        "optimization_id": optimization_id,
        "status": "completed" if best is not None else "no_eligible_candidate",
        "code_version": code_version,
        "database": str(target),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "date_range": {"start": start_date, "end": end_date},
        "snapshot_set_hash": snapshot_set_hash,
        "objective": objective,
        "candidate_count": len(results),
        "eligible_candidate_count": sum(item["status"] == "passed" for item in results),
        "best_candidate_id": best["candidate_id"] if best else None,
        "best_portfolio_run_id": best["portfolio_run_id"] if best else None,
        "best_candidate": best,
        "results": ranked,
        "production_ready": False,
        "production_changed": False,
        "completed_at_utc": _utc_now(),
    }
    best_candidate_path: str | None = None
    if best is not None:
        best_payload = {
            "artifact_type": "kinace_v3_optimized_candidate",
            "optimization_id": optimization_id,
            "candidate_id": best["candidate_id"],
            "portfolio_run_id": best["portfolio_run_id"],
            "model_config": best["model_config"],
            "backtest_config": best["backtest_config"],
            "objective": objective,
            "objective_score": best["objective_score"],
            "components": best["components"],
            "metrics": best["metrics"],
            "production_ready": False,
            "production_changed": False,
        }
        best_path = _write_json(best_payload, resolved_output_dir / "best-candidate.json")
        best_candidate_path = str(best_path)
    report["best_candidate_path"] = best_candidate_path
    written = _write_json(report, artifact_path)
    report["artifact_path"] = str(written)
    report["artifact_sha256"] = sha256_file(written)
    _persist_optimization(
        target,
        optimization_id=optimization_id,
        code_version=code_version,
        status=report["status"],
        snapshot_set_hash=snapshot_set_hash,
        candidate_count=len(results),
        best_candidate_id=report["best_candidate_id"],
        best_portfolio_run_id=report["best_portfolio_run_id"],
        objective=objective,
        artifact_path=written,
        artifact_sha256=report["artifact_sha256"],
        candidates=ranked,
    )
    return report


__all__ = [
    "DEFAULT_OBJECTIVE",
    "DEFAULT_OPTIMIZATION_OUTPUT_DIR",
    "DEFAULT_OPTIMIZATION_REPORT_DIR",
    "MAX_OPTIMIZATION_CANDIDATES",
    "OBJECTIVE_SCHEMA_VERSION",
    "OptimizationError",
    "SCHEMA_VERSION",
    "initialize_optimization_schema",
    "normalize_objective_config",
    "normalize_optimization_payload",
    "run_parameter_optimization",
    "score_portfolio_objective",
]
