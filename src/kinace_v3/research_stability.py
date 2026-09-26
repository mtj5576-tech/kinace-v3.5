"""Rolling-window and robustness validation for research challengers."""

from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .optimization import normalize_objective_config, score_portfolio_objective
from .portfolio_backtest import load_market_bars, run_portfolio_backtest
from .research_decision import load_candidate_oos_returns
from .research_statistics import analyze_return_series, build_trial_ledger
from .score_replay import validate_candidate_config
from .trial_backtest import initialize_trial_backtest_schema, normalize_backtest_config


SCHEMA_VERSION = 1
TRADE_COUNT_POLICY_VERSION = "kinace-v3.3-d2-candidate-level-trade-count-v1"
CANDIDATE_TRADE_COUNT_SCOPE = "walk_forward_candidate_aggregate"
DEFAULT_STABILITY_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_stability"
DEFAULT_STABILITY_REPORT_DIR = PROJECT_ROOT / "reports" / "research_stability"
DEFAULT_BASELINE_CANDIDATE = PROJECT_ROOT / "reports" / "baseline_v32_candidate.json"
DEFAULT_BASELINE_CONFIG = PROJECT_ROOT / "reports" / "baseline_v32_backtest_config.json"
DEFAULT_STABILITY_CONFIG: dict[str, Any] = {
    "min_oos_windows": 8,
    "min_noninferior_rate": 0.75,
    "min_candidate_gate_pass_rate": 0.75,
    "max_drawdown_regression": 0.10,
    "paired_return_epsilon": 0.0,
    "paired_profit_factor_epsilon": 0.0,
    "paired_max_drawdown_upper_bound": 0.10,
    "bootstrap_iterations": 5_000,
    "confidence_level": 0.95,
    "bootstrap_seed": 0,
    "assume_recorded_trials_complete": True,
    "max_feature_weight": 0.75,
    "max_feature_weight_binding": True,
    "cost_slippage_bps": [5.0, 10.0, 20.0],
    "cost_min_positive_rate": 0.667,
    "market_filter_min_positive_rate": 0.75,
    "market_filter_max_return_range": 0.50,
    "trade_count_policy_version": TRADE_COUNT_POLICY_VERSION,
    "trade_count_scope": CANDIDATE_TRADE_COUNT_SCOPE,
    "candidate_min_trade_count": 30,
    "fold_min_trade_count": 1,
    "min_active_fold_rate": 1.0,
}
COST_CONFIG_KEYS = (
    "commission_rate",
    "min_commission",
    "stamp_duty_rate",
    "transfer_fee_rate",
    "slippage_bps",
)
TRADE_KEY_FIELDS = ("code", "decision_date", "entry_date", "exit_date")
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


class ResearchStabilityError(ValueError):
    """Raised when a rolling stability validation cannot run safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchStabilityError(f"{field} must be an object")
    return value


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchStabilityError(f"cannot load {field}: {exc}") from exc


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
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    return target


def _connect(database: str | Path) -> sqlite3.Connection:
    target = validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def normalize_stability_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = dict(config or {})
    unknown = sorted(set(source) - set(DEFAULT_STABILITY_CONFIG))
    if unknown:
        raise ResearchStabilityError(
            "unknown stability configuration keys: " + ", ".join(unknown)
        )
    result = deepcopy(DEFAULT_STABILITY_CONFIG)
    result.update(source)
    for key in (
        "min_noninferior_rate",
        "min_candidate_gate_pass_rate",
        "max_drawdown_regression",
        "paired_return_epsilon",
        "paired_profit_factor_epsilon",
        "paired_max_drawdown_upper_bound",
        "confidence_level",
        "max_feature_weight",
        "cost_min_positive_rate",
        "market_filter_min_positive_rate",
        "market_filter_max_return_range",
        "min_active_fold_rate",
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResearchStabilityError(f"{key} must be numeric")
        result[key] = float(value)
    result["min_oos_windows"] = int(result["min_oos_windows"])
    result["bootstrap_iterations"] = int(result["bootstrap_iterations"])
    result["bootstrap_seed"] = int(result["bootstrap_seed"])
    result["candidate_min_trade_count"] = int(
        result["candidate_min_trade_count"]
    )
    result["fold_min_trade_count"] = int(result["fold_min_trade_count"])
    if result["min_oos_windows"] <= 0:
        raise ResearchStabilityError("min_oos_windows must be positive")
    if result["bootstrap_iterations"] <= 0:
        raise ResearchStabilityError("bootstrap_iterations must be positive")
    if result["candidate_min_trade_count"] <= 0:
        raise ResearchStabilityError("candidate_min_trade_count must be positive")
    if result["fold_min_trade_count"] <= 0:
        raise ResearchStabilityError("fold_min_trade_count must be positive")
    if not 0 < result["min_active_fold_rate"] <= 1:
        raise ResearchStabilityError("min_active_fold_rate must be in (0, 1]")
    if not 0 < result["confidence_level"] < 1:
        raise ResearchStabilityError("confidence_level must be in (0, 1)")
    if result["paired_max_drawdown_upper_bound"] < 0:
        raise ResearchStabilityError(
            "paired_max_drawdown_upper_bound must be non-negative"
        )
    if not isinstance(result["assume_recorded_trials_complete"], bool):
        raise ResearchStabilityError(
            "assume_recorded_trials_complete must be boolean"
        )
    if not isinstance(result["max_feature_weight_binding"], bool):
        raise ResearchStabilityError("max_feature_weight_binding must be boolean")
    slippage = result["cost_slippage_bps"]
    if not isinstance(slippage, Sequence) or isinstance(slippage, (str, bytes)):
        raise ResearchStabilityError("cost_slippage_bps must be a list")
    values = sorted({float(value) for value in slippage})
    if not values or values[0] < 0:
        raise ResearchStabilityError("cost_slippage_bps must contain non-negative values")
    result["cost_slippage_bps"] = values
    if result["trade_count_policy_version"] != TRADE_COUNT_POLICY_VERSION:
        raise ResearchStabilityError(
            "unsupported trade_count_policy_version: "
            + str(result["trade_count_policy_version"])
        )
    if result["trade_count_scope"] != CANDIDATE_TRADE_COUNT_SCOPE:
        raise ResearchStabilityError(
            "unsupported trade_count_scope: " + str(result["trade_count_scope"])
        )
    return result


def objective_with_fold_trade_minimum(
    objective: Mapping[str, Any],
    fold_min_trade_count: int,
) -> dict[str, Any]:
    """Return an objective that applies the D2 fold-level trade floor."""
    threshold = int(fold_min_trade_count)
    if threshold <= 0:
        raise ResearchStabilityError("fold_min_trade_count must be positive")
    payload = deepcopy(dict(objective))
    constraints = payload.get("constraints")
    if isinstance(constraints, Mapping) and isinstance(
        constraints.get("min_trade_count"), Mapping
    ):
        updated_constraints = deepcopy(dict(constraints))
        updated_constraint = deepcopy(
            dict(_mapping(updated_constraints["min_trade_count"], "min_trade_count"))
        )
        updated_constraint["threshold"] = threshold
        updated_constraints["min_trade_count"] = updated_constraint
        payload["constraints"] = updated_constraints
        return payload
    hard_gates = payload.get("hard_gates")
    if isinstance(hard_gates, Mapping):
        updated_gates = deepcopy(dict(hard_gates))
        updated_gates["min_trade_count"] = threshold
        payload["hard_gates"] = updated_gates
        return payload
    raise ResearchStabilityError(
        "objective must define constraints.min_trade_count or hard_gates.min_trade_count"
    )


def trade_count_policy_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    settings = normalize_stability_config(config)
    return {
        "version": settings["trade_count_policy_version"],
        "scope": settings["trade_count_scope"],
        "candidate_min_trade_count": settings["candidate_min_trade_count"],
        "fold_min_trade_count": settings["fold_min_trade_count"],
        "min_active_fold_rate": settings["min_active_fold_rate"],
    }


def summarize_candidate_trade_count(
    fold_trade_counts: Sequence[int],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Adjudicate the frozen candidate-level walk-forward trade-count policy."""
    settings = normalize_stability_config(config)
    counts = [int(value) for value in fold_trade_counts]
    if any(value < 0 for value in counts):
        raise ResearchStabilityError("fold trade counts must be non-negative")
    total = sum(counts)
    active_folds = sum(
        value >= settings["fold_min_trade_count"] for value in counts
    )
    active_rate = active_folds / len(counts) if counts else 0.0
    checks = {
        "candidate_trade_count": total
        >= settings["candidate_min_trade_count"],
        "active_fold_rate": active_rate
        >= settings["min_active_fold_rate"],
    }
    identity = trade_count_policy_identity(settings)
    return {
        **identity,
        "policy_id": sha256_bytes(
            canonical_json_bytes(identity, trailing_newline=False)
        ),
        "fold_count": len(counts),
        "fold_trade_counts": counts,
        "candidate_trade_count": total,
        "active_folds": active_folds,
        "active_fold_rate": round(active_rate, 8),
        "checks": checks,
        "passed": all(checks.values()),
    }


def initialize_stability_schema(database: str | Path | None = None) -> Path:
    target = initialize_trial_backtest_schema(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_stability_runs (
                stability_id TEXT PRIMARY KEY,
                loop_id TEXT NOT NULL,
                candidate_signature TEXT NOT NULL,
                status TEXT NOT NULL,
                window_count INTEGER NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                summary_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_research_stability_loop
                ON research_stability_runs(loop_id, created_at_utc);
            """
        )
    return target


def _metric_number(metrics: Mapping[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _paired_delta_statistics(
    values: Sequence[float],
    *,
    bootstrap_iterations: int,
    confidence_level: float,
    seed: int,
) -> dict[str, Any]:
    clean = [float(value) for value in values]
    if not clean:
        return {
            "observations": 0,
            "mean": None,
            "stdev": None,
            "standard_error": None,
            "median": None,
            "min": None,
            "max": None,
            "paired_t_stat": None,
            "paired_t_p_value_normal_approx": None,
            "sign_test_p_value": None,
            "bootstrap_confidence_interval": {
                "lower": None,
                "median": None,
                "upper": None,
            },
        }
    mean = statistics.fmean(clean)
    deviation = statistics.stdev(clean) if len(clean) >= 2 else 0.0
    standard_error = deviation / math.sqrt(len(clean)) if clean else None
    t_stat = mean / standard_error if standard_error and standard_error > 0 else None
    t_p_value = (
        2.0 * (1.0 - statistics.NormalDist().cdf(abs(t_stat)))
        if t_stat is not None
        else None
    )
    positive = sum(value > 0 for value in clean)
    negative = sum(value < 0 for value in clean)
    nonzero = positive + negative
    if nonzero:
        tail = min(positive, negative)
        probability = sum(math.comb(nonzero, index) for index in range(tail + 1))
        sign_p_value = min(1.0, 2.0 * probability / (2**nonzero))
    else:
        sign_p_value = 1.0
    rng = random.Random(seed)
    bootstrap_means = [
        statistics.fmean([rng.choice(clean) for _ in clean])
        for _ in range(bootstrap_iterations)
    ]
    tail_probability = (1.0 - confidence_level) / 2.0

    def quantile(probability: float) -> float:
        ordered = sorted(bootstrap_means)
        position = (len(ordered) - 1) * probability
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    return {
        "observations": len(clean),
        "mean": mean,
        "stdev": deviation,
        "standard_error": standard_error,
        "median": statistics.median(clean),
        "min": min(clean),
        "max": max(clean),
        "paired_t_stat": t_stat,
        "paired_t_p_value_normal_approx": t_p_value,
        "sign_test_p_value": sign_p_value,
        "bootstrap_confidence_interval": {
            "lower": quantile(tail_probability),
            "median": quantile(0.5),
            "upper": quantile(1.0 - tail_probability),
        },
    }


def _trade_identity(trade: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(trade.get(field) or "") for field in TRADE_KEY_FIELDS)


def _compare_reports(
    *,
    candidate_report: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    objective: Mapping[str, Any],
    max_drawdown_regression: float,
    fold_min_trade_count: int = 1,
) -> dict[str, Any]:
    candidate_range = _mapping(
        candidate_report.get("date_range") or {}, "candidate.date_range"
    )
    baseline_range = _mapping(
        baseline_report.get("date_range") or {}, "baseline.date_range"
    )
    candidate_metrics = _mapping(
        candidate_report.get("metrics") or {}, "candidate.metrics"
    )
    baseline_metrics = _mapping(
        baseline_report.get("metrics") or {}, "baseline.metrics"
    )
    candidate_manifest = _mapping(
        candidate_report.get("snapshot_manifest") or {},
        "candidate.snapshot_manifest",
    )
    baseline_manifest = _mapping(
        baseline_report.get("snapshot_manifest") or {},
        "baseline.snapshot_manifest",
    )
    candidate_snapshot_set = str(candidate_manifest.get("snapshot_set_id") or "")
    baseline_snapshot_set = str(baseline_manifest.get("snapshot_set_id") or "")
    candidate_config = candidate_report.get("config") or {}
    baseline_config = baseline_report.get("config") or {}
    cost_fields_equal = all(
        candidate_config.get(key) == baseline_config.get(key)
        for key in COST_CONFIG_KEYS
    )
    fold_objective = objective_with_fold_trade_minimum(
        objective,
        fold_min_trade_count,
    )
    candidate_objective = score_portfolio_objective(
        candidate_metrics,
        fold_objective,
    )
    baseline_objective = score_portfolio_objective(baseline_metrics, objective)
    candidate_return = _metric_number(candidate_metrics, "total_return")
    baseline_return = _metric_number(baseline_metrics, "total_return")
    candidate_pf = _metric_number(candidate_metrics, "profit_factor")
    baseline_pf = _metric_number(baseline_metrics, "profit_factor")
    candidate_dd = _metric_number(candidate_metrics, "max_drawdown")
    baseline_dd = _metric_number(baseline_metrics, "max_drawdown")
    candidate_trades = Counter(
        _trade_identity(item)
        for item in candidate_report.get("trades") or []
        if isinstance(item, Mapping)
    )
    baseline_trades = Counter(
        _trade_identity(item)
        for item in baseline_report.get("trades") or []
        if isinstance(item, Mapping)
    )
    common = candidate_trades & baseline_trades
    union = candidate_trades | baseline_trades
    common_count = sum(common.values())
    union_count = sum(union.values())
    delta = {
        "total_return": round(candidate_return - baseline_return, 8)
        if candidate_return is not None and baseline_return is not None
        else None,
        "profit_factor": round(candidate_pf - baseline_pf, 8)
        if candidate_pf is not None and baseline_pf is not None
        else None,
        "max_drawdown": round(candidate_dd - baseline_dd, 8)
        if candidate_dd is not None and baseline_dd is not None
        else None,
        "trade_count": int(candidate_metrics.get("trade_count") or 0)
        - int(baseline_metrics.get("trade_count") or 0),
    }
    checks = {
        "same_window": candidate_range == baseline_range,
        "same_snapshot_set": bool(
            candidate_snapshot_set
            and baseline_snapshot_set
            and candidate_snapshot_set == baseline_snapshot_set
        ),
        "cost_fields_equal": cost_fields_equal,
        "candidate_gate_passed": candidate_objective["status"] == "passed",
        "return_not_worse": bool(
            delta["total_return"] is not None and delta["total_return"] >= 0
        ),
        "profit_factor_not_worse": bool(
            delta["profit_factor"] is not None and delta["profit_factor"] >= 0
        ),
        "drawdown_not_materially_worse": bool(
            delta["max_drawdown"] is not None
            and delta["max_drawdown"] <= max_drawdown_regression
        ),
    }
    noninferior = all(
        checks[key]
        for key in (
            "same_window",
            "same_snapshot_set",
            "cost_fields_equal",
            "candidate_gate_passed",
            "return_not_worse",
            "profit_factor_not_worse",
        )
    )
    return {
        "window": {
            "start": str(candidate_range.get("start") or ""),
            "end": str(candidate_range.get("end") or ""),
            "sessions": int(candidate_metrics.get("trading_days") or 0),
        },
        "snapshot_set_id": candidate_snapshot_set,
        "candidate_run_id": candidate_report.get("run_id"),
        "baseline_run_id": baseline_report.get("run_id"),
        "candidate_metrics": dict(candidate_metrics),
        "baseline_metrics": dict(baseline_metrics),
        "candidate_objective": candidate_objective,
        "baseline_objective": baseline_objective,
        "metric_delta_challenger_minus_baseline": delta,
        "trade_overlap": {
            "baseline_trades": sum(baseline_trades.values()),
            "challenger_trades": sum(candidate_trades.values()),
            "common_trades": common_count,
            "union_trades": union_count,
            "jaccard": round(common_count / union_count, 8) if union_count else 1.0,
        },
        "checks": checks,
        "verdict": "challenger_noninferior" if noninferior else "challenger_not_qualified",
    }


def _candidate_window_reports(
    loop_report: Mapping[str, Any],
    *,
    include_holdout: bool = False,
) -> list[dict[str, Any]]:
    best = _mapping(loop_report.get("best_candidate"), "loop_report.best_candidate")
    windows: list[dict[str, Any]] = []
    for fold in best.get("folds") or []:
        if not isinstance(fold, Mapping):
            continue
        path = str(fold.get("portfolio_output_path") or "")
        if not path:
            continue
        windows.append(
            {
                "id": f"fold-{int(fold.get('fold') or 0):02d}",
                "kind": "walk_forward_fold",
                "fold": int(fold.get("fold") or 0),
                "path": path,
                "report": _mapping(
                    _load_json(path, f"fold {fold.get('fold')} report"),
                    "fold report",
                ),
            }
        )
    holdout = _mapping(loop_report.get("holdout"), "loop_report.holdout")
    path = str(holdout.get("portfolio_output_path") or "")
    if include_holdout and path:
        windows.append(
            {
                "id": "final-holdout",
                "kind": "final_holdout",
                "fold": None,
                "path": path,
                "report": _mapping(_load_json(path, "holdout report"), "holdout report"),
            }
        )
    if not windows:
        raise ResearchStabilityError("research loop has no candidate validation reports")
    return windows


def _candidate_holdout_report(loop_report: Mapping[str, Any]) -> dict[str, Any]:
    holdout = _mapping(loop_report.get("holdout"), "loop_report.holdout")
    path = str(holdout.get("portfolio_output_path") or "")
    if not path:
        raise ResearchStabilityError("research loop has no final holdout report")
    return {
        "id": "final-holdout",
        "kind": "final_holdout",
        "fold": None,
        "path": path,
        "report": _mapping(_load_json(path, "holdout report"), "holdout report"),
    }


def _protocol_stability_config(loop_report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    protocol = loop_report.get("evaluation_protocol")
    if not isinstance(protocol, Mapping):
        return None
    identity = protocol.get("protocol_identity")
    if not isinstance(identity, Mapping):
        return None
    evaluation = identity.get("evaluation_config")
    if not isinstance(evaluation, Mapping):
        return None
    stability = evaluation.get("stability")
    return stability if isinstance(stability, Mapping) else None


def summarize_weight_stability(
    loop_report: Mapping[str, Any],
    *,
    max_feature_weight: float,
    max_feature_weight_binding: bool = False,
) -> dict[str, Any]:
    best = _mapping(loop_report.get("best_candidate"), "loop_report.best_candidate")
    sources: list[tuple[str, Mapping[str, Any]]] = []
    for fold in best.get("folds") or []:
        if not isinstance(fold, Mapping):
            continue
        training = fold.get("training")
        if isinstance(training, Mapping) and isinstance(
            training.get("trained_weights"), Mapping
        ):
            sources.append(
                (
                    f"fold-{int(fold.get('fold') or 0):02d}",
                    training["trained_weights"],
                )
            )
    final_training = best.get("final_training")
    if isinstance(final_training, Mapping) and isinstance(
        final_training.get("trained_weights"), Mapping
    ):
        sources.append(("final-holdout", final_training["trained_weights"]))
    by_feature: dict[str, list[float]] = {}
    for _, weights in sources:
        for name in FEATURE_NAMES:
            value = weights.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            by_feature.setdefault(name, []).append(float(value))
    feature_summary = []
    for name in FEATURE_NAMES:
        values = by_feature.get(name, [])
        feature_summary.append(
            {
                "feature": name,
                "count": len(values),
                "min": round(min(values), 8) if values else None,
                "max": round(max(values), 8) if values else None,
                "mean": round(statistics.fmean(values), 8) if values else None,
                "stdev": round(statistics.stdev(values), 8)
                if len(values) >= 2
                else 0.0,
            }
        )
    dominant = max(
        feature_summary,
        key=lambda item: float(item["max"] or 0.0),
        default=None,
    )
    maximum = float((dominant or {}).get("max") or 0.0)
    dispersion = (
        statistics.fmean(float(item["stdev"] or 0.0) for item in feature_summary)
        if feature_summary
        else 0.0
    )
    checks = {
        "training_windows_available": len(sources) >= 2,
        "feature_weight_within_limit": maximum <= max_feature_weight,
    }
    budget_exceeded = not checks["feature_weight_within_limit"]
    if max_feature_weight_binding:
        status = "stable" if all(checks.values()) else "review_required"
    else:
        status = "report_only"
    return {
        "status": status,
        "binding": bool(max_feature_weight_binding),
        "training_windows": len(sources),
        "dominant_feature": (dominant or {}).get("feature"),
        "max_feature_weight": round(maximum, 8),
        "max_feature_weight_limit": max_feature_weight,
        "budget_exceeded": budget_exceeded,
        "mean_feature_weight_stdev": round(dispersion, 8),
        "features": feature_summary,
        "checks": checks,
    }


def _market_filter_variants(base: Mapping[str, Any]) -> list[dict[str, Any]]:
    variants = [
        {"label": "filter_disabled", "market_filter_enabled": False},
        {
            "label": "sessions_05_min_-001",
            "market_filter_enabled": True,
            "market_filter_sessions": 5,
            "min_market_return": -0.01,
        },
        {
            "label": "sessions_05_min_000",
            "market_filter_enabled": True,
            "market_filter_sessions": 5,
            "min_market_return": 0.0,
        },
        {
            "label": "sessions_10_min_-001",
            "market_filter_enabled": True,
            "market_filter_sessions": 10,
            "min_market_return": -0.01,
        },
        {
            "label": "sessions_10_min_000",
            "market_filter_enabled": True,
            "market_filter_sessions": 10,
            "min_market_return": 0.0,
        },
        {
            "label": "sessions_10_min_001",
            "market_filter_enabled": True,
            "market_filter_sessions": 10,
            "min_market_return": 0.01,
        },
        {
            "label": "sessions_20_min_000",
            "market_filter_enabled": True,
            "market_filter_sessions": 20,
            "min_market_return": 0.0,
        },
    ]
    result = []
    for variant in variants:
        config = dict(base)
        config.update({key: value for key, value in variant.items() if key != "label"})
        result.append(
            {"label": variant["label"], "config": normalize_backtest_config(config)}
        )
    return result


def _markdown_report(payload: Mapping[str, Any]) -> str:
    rolling = payload["rolling_windows"]
    cost = payload["cost_sensitivity"]
    market_filter = payload["market_filter_sensitivity"]
    weights = payload["weight_stability"]
    lines = [
        "# Kinace Research Stability Validation",
        "",
        f"- Stability ID: `{payload['stability_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Loop: `{payload['loop_id']}`",
        f"- Candidate: `{payload['candidate_signature']}`",
        f"- Windows: `{rolling['window_count']}`",
        "",
        "## Rolling Windows",
        "",
        "| Window | Kind | Return | Baseline | PF | Baseline PF | Verdict |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for item in rolling["windows"]:
        comparison = item["comparison"]
        lines.append(
            f"| `{item['window']['start']}` to `{item['window']['end']}` | "
            f"`{item['kind']}` | {comparison['candidate_metrics'].get('total_return')} | "
            f"{comparison['baseline_metrics'].get('total_return')} | "
            f"{comparison['candidate_metrics'].get('profit_factor')} | "
            f"{comparison['baseline_metrics'].get('profit_factor')} | "
            f"`{comparison['verdict']}` |"
        )
    trade_counts = rolling["trade_count_adjudication"]
    lines.extend(
        [
            "",
            "## Candidate Trade Count",
            "",
            f"- Policy: `{trade_counts['version']}`",
            f"- Scope: `{trade_counts['scope']}`",
            "- Candidate trades: "
            f"`{trade_counts['candidate_trade_count']} >= "
            f"{trade_counts['candidate_min_trade_count']}`",
            "- Active folds: "
            f"`{trade_counts['active_folds']}/{trade_counts['fold_count']} >= "
            f"{trade_counts['min_active_fold_rate']}`",
            f"- Fold minimum: `{trade_counts['fold_min_trade_count']}`",
            f"- Adjudication: `{trade_counts['passed']}`",
        ]
    )
    lines.extend(
        [
            "",
            "## Paired Delta Confidence Intervals",
            "",
            "| Metric | Mean | CI lower | CI upper | Sign-test p-value |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for metric, statistics_payload in rolling["paired_delta_statistics"].items():
        interval = statistics_payload["bootstrap_confidence_interval"]
        lines.append(
            f"| `{metric}` | {statistics_payload['mean']} | {interval['lower']} | "
            f"{interval['upper']} | {statistics_payload['sign_test_p_value']} |"
        )
    lines.extend(
        [
            "",
            "## Cost Sensitivity",
            "",
            "| Slippage bps | Candidate return | Baseline return | Return delta | PF delta | Verdict |",
            "|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in cost["cases"]:
        comparison = item["comparison"]
        lines.append(
            f"| {item['slippage_bps']} | "
            f"{comparison['candidate_metrics'].get('total_return')} | "
            f"{comparison['baseline_metrics'].get('total_return')} | "
            f"{comparison['metric_delta_challenger_minus_baseline'].get('total_return')} | "
            f"{comparison['metric_delta_challenger_minus_baseline'].get('profit_factor')} | "
            f"`{comparison['verdict']}` |"
        )
    lines.extend(
        [
            "",
            f"- Cost return-delta CI: `{cost['return_delta_distribution']['bootstrap_confidence_interval']}`",
        ]
    )
    lines.extend(
        [
            "",
            "## Market Filter Sensitivity",
            "",
            "| Variant | Return | PF | Gate |",
            "|---|---:|---:|---|",
        ]
    )
    for item in market_filter["cases"]:
        lines.append(
            f"| `{item['label']}` | {item['metrics'].get('total_return')} | "
            f"{item['metrics'].get('profit_factor')} | `{item['objective']['status']}` |"
        )
    lines.extend(
        [
            "",
            f"- Market-filter return distribution: `{market_filter['return_distribution']}`",
            f"- Response-surface cases: `{len(market_filter['response_surface'])}`",
        ]
    )
    statistical = payload.get("statistical_adjudication") or {}
    if statistical.get("status") == "ready":
        trial_ledger = statistical["trial_ledger"]
        statistics_payload = statistical["statistics"]
        lines.extend(
            [
                "",
                "## Statistical Adjudication",
                "",
                f"- N_trials_total: `{trial_ledger['N_trials_total']}`",
                f"- N_eff_measured: `{statistics_payload['effective_sample_size']['N_eff_measured']}`",
                f"- Sharpe CI: `{statistics_payload['bootstrap_confidence_interval']['annualized_sharpe']}`",
                f"- DSR p-value: `{statistics_payload['deflated_sharpe_ratio'].get('p_value')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Weight Stability",
            "",
            f"- Status: `{weights['status']}`",
            f"- Binding: `{weights['binding']}`",
            f"- Dominant feature: `{weights.get('dominant_feature')}`",
            f"- Max feature weight: `{weights['max_feature_weight']}`",
            f"- Limit: `{weights['max_feature_weight_limit']}`",
            f"- Budget exceeded: `{weights['budget_exceeded']}`",
            f"- Mean feature-weight stdev: `{weights['mean_feature_weight_stdev']}`",
            "",
            "## Checks",
            "",
            "| Check | Passed | Detail |",
            "|---|---:|---|",
        ]
    )
    for item in payload["checks"]:
        lines.append(f"| `{item['id']}` | {item['passed']} | {item['detail']} |")
    report_only_checks = payload.get("report_only_checks") or []
    if report_only_checks:
        lines.extend(
            [
                "",
                "## Report-Only Checks",
                "",
                "| Check | Passed | Detail |",
                "|---|---:|---|",
            ]
        )
        for item in report_only_checks:
            lines.append(f"| `{item['id']}` | {item['passed']} | {item['detail']} |")
    lines.append("")
    return "\n".join(lines)


def run_research_stability(
    database: str | Path | None,
    research_report_path: str | Path,
    *,
    snapshot_dir: str | Path,
    baseline_candidate_path: str | Path = DEFAULT_BASELINE_CANDIDATE,
    baseline_config_path: str | Path = DEFAULT_BASELINE_CONFIG,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_STABILITY_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_STABILITY_REPORT_DIR,
    code_version: str = "kinace-v3",
    config: Mapping[str, Any] | None = None,
    portfolio_runner: Callable[..., Mapping[str, Any]] = run_portfolio_backtest,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    loop_report = _mapping(
        _load_json(research_report_path, "research loop report"),
        "research loop report",
    )
    protocol_stability = _protocol_stability_config(loop_report)
    if config is None and protocol_stability is not None:
        if "trade_count_policy_version" not in protocol_stability:
            raise ResearchStabilityError(
                "loop protocol predates the candidate-level trade-count policy; "
                "historical requalification requires a new protocol and candidate"
            )
        config = protocol_stability
    settings = normalize_stability_config(config)
    loop_id = str(loop_report.get("loop_id") or "")
    best = _mapping(loop_report.get("best_candidate"), "loop_report.best_candidate")
    candidate_signature = str(best.get("signature") or "")
    if not loop_id or not candidate_signature:
        raise ResearchStabilityError(
            "research loop report is missing loop or candidate identity"
        )
    candidate_model = validate_candidate_config(
        _mapping(best.get("model_config"), "best_candidate.model_config")
    )
    candidate_config = normalize_backtest_config(
        _mapping(best.get("backtest_config"), "best_candidate.backtest_config")
    )
    baseline_model = validate_candidate_config(
        _mapping(
            _load_json(baseline_candidate_path, "baseline candidate"),
            "baseline candidate",
        )
    )
    baseline_config = normalize_backtest_config(
        _mapping(_load_json(baseline_config_path, "baseline config"), "baseline config")
    )
    objective = _mapping(loop_report.get("objective") or {}, "loop_report.objective")
    window_reports = _candidate_window_reports(loop_report)
    bars = load_market_bars(target)
    stability_dir = Path(output_dir).resolve(strict=False)

    rolling_windows: list[dict[str, Any]] = []
    baseline_by_id: dict[str, Mapping[str, Any]] = {}
    for index, window in enumerate(window_reports):
        baseline_output = stability_dir / "baseline" / f"{index:02d}-{window['id']}.json"
        candidate_report = _mapping(window["report"], "candidate window report")
        candidate_range = _mapping(
            candidate_report.get("date_range") or {}, "candidate date range"
        )
        baseline_report = portfolio_runner(
            target,
            baseline_model,
            snapshot_dir,
            start_date=str(candidate_range.get("start") or ""),
            end_date=str(candidate_range.get("end") or ""),
            bars=bars,
            config=baseline_config,
            output_path=baseline_output,
            code_version=code_version,
        )
        comparison = _compare_reports(
            candidate_report=candidate_report,
            baseline_report=baseline_report,
            objective=objective,
            max_drawdown_regression=settings["max_drawdown_regression"],
            fold_min_trade_count=settings["fold_min_trade_count"],
        )
        baseline_by_id[window["id"]] = baseline_report
        rolling_windows.append(
            {
                "id": window["id"],
                "kind": window["kind"],
                "window": comparison["window"],
                "candidate_report_path": window["path"],
                "baseline_report_path": baseline_report.get("output_path"),
                "comparison": comparison,
            }
        )
    noninferior_count = sum(
        item["comparison"]["verdict"] == "challenger_noninferior"
        for item in rolling_windows
    )
    gate_pass_count = sum(
        item["comparison"]["checks"]["candidate_gate_passed"]
        for item in rolling_windows
    )
    trade_count_adjudication = summarize_candidate_trade_count(
        [
            int(
                item["comparison"]["candidate_metrics"].get("trade_count") or 0
            )
            for item in rolling_windows
        ],
        settings,
    )
    return_deltas = [
        float(
            item["comparison"]["metric_delta_challenger_minus_baseline"][
                "total_return"
            ]
        )
        for item in rolling_windows
        if item["comparison"]["metric_delta_challenger_minus_baseline"][
            "total_return"
        ]
        is not None
    ]
    profit_factor_deltas = [
        float(
            item["comparison"]["metric_delta_challenger_minus_baseline"][
                "profit_factor"
            ]
        )
        for item in rolling_windows
        if item["comparison"]["metric_delta_challenger_minus_baseline"][
            "profit_factor"
        ]
        is not None
    ]
    drawdown_deltas = [
        float(
            item["comparison"]["metric_delta_challenger_minus_baseline"][
                "max_drawdown"
            ]
        )
        for item in rolling_windows
        if item["comparison"]["metric_delta_challenger_minus_baseline"][
            "max_drawdown"
        ]
        is not None
    ]
    paired_delta_statistics = {
        "total_return": _paired_delta_statistics(
            return_deltas,
            bootstrap_iterations=settings["bootstrap_iterations"],
            confidence_level=settings["confidence_level"],
            seed=settings["bootstrap_seed"],
        ),
        "profit_factor": _paired_delta_statistics(
            profit_factor_deltas,
            bootstrap_iterations=settings["bootstrap_iterations"],
            confidence_level=settings["confidence_level"],
            seed=settings["bootstrap_seed"] + 1,
        ),
        "max_drawdown": _paired_delta_statistics(
            drawdown_deltas,
            bootstrap_iterations=settings["bootstrap_iterations"],
            confidence_level=settings["confidence_level"],
            seed=settings["bootstrap_seed"] + 2,
        ),
    }
    failing_windows = [
        {
            "id": item["id"],
            "window": item["window"],
            "total_return_delta": item["comparison"][
                "metric_delta_challenger_minus_baseline"
            ]["total_return"],
            "profit_factor_delta": item["comparison"][
                "metric_delta_challenger_minus_baseline"
            ]["profit_factor"],
            "max_drawdown_delta": item["comparison"][
                "metric_delta_challenger_minus_baseline"
            ]["max_drawdown"],
        }
        for item in rolling_windows
        if (
            float(
                item["comparison"]["metric_delta_challenger_minus_baseline"][
                    "total_return"
                ]
                or 0.0
            )
            < -settings["paired_return_epsilon"]
            or float(
                item["comparison"]["metric_delta_challenger_minus_baseline"][
                    "profit_factor"
                ]
                or 0.0
            )
            < -settings["paired_profit_factor_epsilon"]
            or float(
                item["comparison"]["metric_delta_challenger_minus_baseline"][
                    "max_drawdown"
                ]
                or 0.0
            )
            > settings["paired_max_drawdown_upper_bound"]
        )
    ]
    rolling_summary = {
        "window_count": len(rolling_windows),
        "noninferior_count": noninferior_count,
        "noninferior_rate": round(noninferior_count / len(rolling_windows), 8)
        if rolling_windows
        else 0.0,
        "candidate_gate_pass_count": gate_pass_count,
        "candidate_gate_pass_rate": round(gate_pass_count / len(rolling_windows), 8)
        if rolling_windows
        else 0.0,
        "trade_count_adjudication": trade_count_adjudication,
        "median_return_delta": round(statistics.median(return_deltas), 8)
        if return_deltas
        else None,
        "worst_return_delta": round(min(return_deltas), 8) if return_deltas else None,
        "paired_delta_statistics": paired_delta_statistics,
        "failing_windows": failing_windows,
        "windows": rolling_windows,
    }

    final_window = _candidate_holdout_report(loop_report)
    final_candidate_report = _mapping(final_window["report"], "final holdout report")
    final_range = _mapping(
        final_candidate_report.get("date_range") or {}, "final holdout range"
    )
    final_baseline_report = portfolio_runner(
        target,
        baseline_model,
        snapshot_dir,
        start_date=str(final_range.get("start") or ""),
        end_date=str(final_range.get("end") or ""),
        bars=bars,
        config=baseline_config,
        output_path=stability_dir / "baseline" / "final-holdout.json",
        code_version=code_version,
    )
    cost_cases: list[dict[str, Any]] = []
    for slippage in settings["cost_slippage_bps"]:
        if float(slippage) == float(candidate_config["slippage_bps"]):
            candidate_report = final_candidate_report
            baseline_report = final_baseline_report
        else:
            stressed_candidate_config = normalize_backtest_config(
                {**candidate_config, "slippage_bps": float(slippage)}
            )
            stressed_baseline_config = normalize_backtest_config(
                {**baseline_config, "slippage_bps": float(slippage)}
            )
            candidate_report = portfolio_runner(
                target,
                candidate_model,
                snapshot_dir,
                start_date=str(final_range.get("start") or ""),
                end_date=str(final_range.get("end") or ""),
                bars=bars,
                config=stressed_candidate_config,
                output_path=stability_dir
                / "cost"
                / f"candidate-slippage-{int(slippage)}.json",
                code_version=code_version,
            )
            baseline_report = portfolio_runner(
                target,
                baseline_model,
                snapshot_dir,
                start_date=str(final_range.get("start") or ""),
                end_date=str(final_range.get("end") or ""),
                bars=bars,
                config=stressed_baseline_config,
                output_path=stability_dir
                / "cost"
                / f"baseline-slippage-{int(slippage)}.json",
                code_version=code_version,
            )
        comparison = _compare_reports(
            candidate_report=candidate_report,
            baseline_report=baseline_report,
            objective=objective,
            max_drawdown_regression=settings["max_drawdown_regression"],
        )
        cost_cases.append(
            {
                "slippage_bps": float(slippage),
                "comparison": comparison,
            }
        )
    cost_positive_count = sum(
        float(item["comparison"]["candidate_metrics"].get("total_return") or 0.0) > 0
        for item in cost_cases
    )
    cost_noninferior_count = sum(
        item["comparison"]["verdict"] == "challenger_noninferior"
        for item in cost_cases
    )
    cost_return_deltas = [
        float(
            item["comparison"]["metric_delta_challenger_minus_baseline"][
                "total_return"
            ]
        )
        for item in cost_cases
        if item["comparison"]["metric_delta_challenger_minus_baseline"][
            "total_return"
        ]
        is not None
    ]
    cost_distribution = _paired_delta_statistics(
        cost_return_deltas,
        bootstrap_iterations=settings["bootstrap_iterations"],
        confidence_level=settings["confidence_level"],
        seed=settings["bootstrap_seed"] + 10,
    )
    cost_summary = {
        "case_count": len(cost_cases),
        "positive_count": cost_positive_count,
        "positive_rate": round(cost_positive_count / len(cost_cases), 8)
        if cost_cases
        else 0.0,
        "noninferior_count": cost_noninferior_count,
        "noninferior_rate": round(cost_noninferior_count / len(cost_cases), 8)
        if cost_cases
        else 0.0,
        "max_slippage_bps": max(settings["cost_slippage_bps"])
        if settings["cost_slippage_bps"]
        else None,
        "return_delta_distribution": cost_distribution,
        "cases": cost_cases,
    }

    market_filter_cases: list[dict[str, Any]] = []
    for variant in _market_filter_variants(candidate_config):
        report = portfolio_runner(
            target,
            candidate_model,
            snapshot_dir,
            start_date=str(final_range.get("start") or ""),
            end_date=str(final_range.get("end") or ""),
            bars=bars,
            config=variant["config"],
            output_path=stability_dir / "market_filter" / f"{variant['label']}.json",
            code_version=code_version,
        )
        metrics = _mapping(report.get("metrics") or {}, "market filter metrics")
        objective_result = score_portfolio_objective(metrics, objective)
        market_filter_cases.append(
            {
                "label": variant["label"],
                "config": variant["config"],
                "run_id": report.get("run_id"),
                "output_path": report.get("output_path"),
                "metrics": dict(metrics),
                "objective": objective_result,
            }
        )
    positive_filter_count = sum(
        float(item["metrics"].get("total_return") or 0.0) > 0
        for item in market_filter_cases
    )
    filter_returns = [
        float(item["metrics"].get("total_return") or 0.0)
        for item in market_filter_cases
    ]
    filter_profit_factors = [
        float(item["metrics"].get("profit_factor") or 0.0)
        for item in market_filter_cases
    ]
    filter_drawdowns = [
        float(item["metrics"].get("max_drawdown") or 0.0)
        for item in market_filter_cases
    ]
    response_surface = [
        {
            "label": item["label"],
            "market_filter_enabled": item["config"].get("market_filter_enabled"),
            "market_filter_sessions": item["config"].get("market_filter_sessions"),
            "min_market_return": item["config"].get("min_market_return"),
            "total_return": item["metrics"].get("total_return"),
            "profit_factor": item["metrics"].get("profit_factor"),
            "max_drawdown": item["metrics"].get("max_drawdown"),
            "trade_count": item["metrics"].get("trade_count"),
            "objective_status": item["objective"]["status"],
        }
        for item in market_filter_cases
    ]
    market_filter_summary = {
        "case_count": len(market_filter_cases),
        "positive_count": positive_filter_count,
        "positive_rate": round(positive_filter_count / len(market_filter_cases), 8)
        if market_filter_cases
        else 0.0,
        "return_range": round(max(filter_returns) - min(filter_returns), 8)
        if filter_returns
        else 0.0,
        "return_distribution": _paired_delta_statistics(
            filter_returns,
            bootstrap_iterations=settings["bootstrap_iterations"],
            confidence_level=settings["confidence_level"],
            seed=settings["bootstrap_seed"] + 20,
        ),
        "profit_factor_distribution": _paired_delta_statistics(
            filter_profit_factors,
            bootstrap_iterations=settings["bootstrap_iterations"],
            confidence_level=settings["confidence_level"],
            seed=settings["bootstrap_seed"] + 21,
        ),
        "max_drawdown_distribution": _paired_delta_statistics(
            filter_drawdowns,
            bootstrap_iterations=settings["bootstrap_iterations"],
            confidence_level=settings["confidence_level"],
            seed=settings["bootstrap_seed"] + 22,
        ),
        "response_surface": response_surface,
        "cases": market_filter_cases,
    }

    weight_stability = summarize_weight_stability(
        loop_report,
        max_feature_weight=settings["max_feature_weight"],
        max_feature_weight_binding=settings["max_feature_weight_binding"],
    )
    target_sharpe = None
    protocol = loop_report.get("evaluation_protocol")
    if isinstance(protocol, Mapping):
        identity = protocol.get("protocol_identity")
        if isinstance(identity, Mapping):
            evaluation = identity.get("evaluation_config")
            if isinstance(evaluation, Mapping):
                policy = evaluation.get("evaluation_policy")
                if isinstance(policy, Mapping):
                    target_sharpe = policy.get("target_sharpe")
    trial_ledger: dict[str, Any]
    statistical_statistics: dict[str, Any]
    try:
        trial_ledger = build_trial_ledger(
            target,
            loop_id=loop_id,
            assume_recorded_trials_complete=settings[
                "assume_recorded_trials_complete"
            ],
        )
        oos = load_candidate_oos_returns(research_report_path)
        sample_objective = normalize_objective_config(
            loop_report.get("objective")
            if isinstance(loop_report.get("objective"), Mapping)
            else None
        )
        statistical_statistics = analyze_return_series(
            oos["returns"],
            trade_count=int(trade_count_adjudication["candidate_trade_count"]),
            min_observation_sessions=int(
                sample_objective["constraints"]["min_observation_sessions"][
                    "threshold"
                ]
            ),
            min_trade_count=int(
                sample_objective["constraints"]["min_trade_count"]["threshold"]
            ),
            target_sharpe=target_sharpe,
            N_trials=int(trial_ledger["N_trials_total"]),
            bootstrap_iterations=settings["bootstrap_iterations"],
            confidence_level=settings["confidence_level"],
            seed=settings["bootstrap_seed"] + 100,
        )
        statistical_adjudication = {
            "status": "ready",
            "target_sharpe": target_sharpe,
            "trial_ledger": trial_ledger,
            "statistics": statistical_statistics,
        }
    except Exception as exc:
        statistical_adjudication = {
            "status": "unavailable",
            "target_sharpe": target_sharpe,
            "error": str(exc),
        }
    return_ci = paired_delta_statistics["total_return"][
        "bootstrap_confidence_interval"
    ]
    profit_factor_ci = paired_delta_statistics["profit_factor"][
        "bootstrap_confidence_interval"
    ]
    drawdown_ci = paired_delta_statistics["max_drawdown"][
        "bootstrap_confidence_interval"
    ]
    checks = [
        {
            "id": "minimum_oos_windows",
            "passed": len(rolling_windows) >= settings["min_oos_windows"],
            "detail": f"{len(rolling_windows)} >= {settings['min_oos_windows']}",
        },
        {
            "id": "candidate_trade_count",
            "passed": trade_count_adjudication["checks"][
                "candidate_trade_count"
            ],
            "detail": (
                f"{trade_count_adjudication['candidate_trade_count']} >= "
                f"{trade_count_adjudication['candidate_min_trade_count']}"
            ),
        },
        {
            "id": "active_fold_rate",
            "passed": trade_count_adjudication["checks"]["active_fold_rate"],
            "detail": (
                f"{trade_count_adjudication['active_fold_rate']} >= "
                f"{trade_count_adjudication['min_active_fold_rate']}"
            ),
        },
        {
            "id": "paired_return_ci_lower",
            "passed": return_ci["lower"] is not None
            and return_ci["lower"] >= -settings["paired_return_epsilon"],
            "detail": f"{return_ci['lower']} >= {-settings['paired_return_epsilon']}",
        },
        {
            "id": "paired_profit_factor_ci_lower",
            "passed": profit_factor_ci["lower"] is not None
            and profit_factor_ci["lower"]
            >= -settings["paired_profit_factor_epsilon"],
            "detail": f"{profit_factor_ci['lower']} >= {-settings['paired_profit_factor_epsilon']}",
        },
        {
            "id": "paired_drawdown_ci_upper",
            "passed": drawdown_ci["upper"] is not None
            and drawdown_ci["upper"]
            <= settings["paired_max_drawdown_upper_bound"],
            "detail": f"{drawdown_ci['upper']} <= {settings['paired_max_drawdown_upper_bound']}",
        },
        {
            "id": "market_filter_return_range",
            "passed": market_filter_summary["return_range"]
            <= settings["market_filter_max_return_range"],
            "detail": f"{market_filter_summary['return_range']} <= {settings['market_filter_max_return_range']}",
        },
    ]
    report_only_checks = [
        {
            "id": "rolling_noninferior_rate",
            "passed": rolling_summary["noninferior_rate"]
            >= settings["min_noninferior_rate"],
            "detail": f"{rolling_summary['noninferior_rate']} >= {settings['min_noninferior_rate']}",
        },
        {
            "id": "rolling_gate_pass_rate",
            "passed": rolling_summary["candidate_gate_pass_rate"]
            >= settings["min_candidate_gate_pass_rate"],
            "detail": f"{rolling_summary['candidate_gate_pass_rate']} >= {settings['min_candidate_gate_pass_rate']}",
        },
        {
            "id": "cost_positive_rate_report",
            "passed": cost_summary["positive_rate"]
            >= settings["cost_min_positive_rate"],
            "detail": f"{cost_summary['positive_rate']} >= {settings['cost_min_positive_rate']}",
        },
        {
            "id": "market_filter_positive_rate_report",
            "passed": market_filter_summary["positive_rate"]
            >= settings["market_filter_min_positive_rate"],
            "detail": f"{market_filter_summary['positive_rate']} >= {settings['market_filter_min_positive_rate']}",
        },
    ]
    if statistical_adjudication["status"] == "ready":
        statistics_payload = statistical_adjudication["statistics"]
        sharpe_ci = statistics_payload["bootstrap_confidence_interval"][
            "annualized_sharpe"
        ]
        dsr = statistics_payload["deflated_sharpe_ratio"]
        report_only_checks.extend(
            [
                {
                    "id": "cumulative_oos_sharpe_ci_lower_report",
                    "passed": sharpe_ci["lower"] > 0,
                    "detail": f"{sharpe_ci['lower']} > 0",
                },
                {
                    "id": "deflated_sharpe_p_value_report",
                    "passed": dsr.get("computable") is True
                    and dsr.get("p_value") is not None
                    and float(dsr["p_value"]) < 0.05,
                    "detail": str(dsr.get("p_value")),
                },
            ]
        )
    feature_weight_check = {
        "id": "feature_weight_concentration",
        "passed": weight_stability["checks"]["feature_weight_within_limit"],
        "detail": f"{weight_stability['max_feature_weight']} <= {weight_stability['max_feature_weight_limit']}",
    }
    if settings["max_feature_weight_binding"]:
        checks.append(feature_weight_check)
    else:
        report_only_checks.append(feature_weight_check)
    status = "passed" if all(item["passed"] for item in checks) else "review_required"
    identity = {
        "loop_id": loop_id,
        "candidate_signature": candidate_signature,
        "settings": settings,
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "code_version": code_version,
    }
    stability_id = sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(report_dir).resolve(strict=False)
        / f"research-stability-{stability_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else artifact_path.with_suffix(".md")
    )
    payload = {
        "artifact_type": "kinace_v3_research_stability",
        "schema_version": SCHEMA_VERSION,
        "stability_id": stability_id,
        "status": status,
        "loop_id": loop_id,
        "candidate_signature": candidate_signature,
        "database": str(target),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "settings": settings,
        "rolling_windows": rolling_summary,
        "trade_count_adjudication": trade_count_adjudication,
        "cost_sensitivity": cost_summary,
        "market_filter_sensitivity": market_filter_summary,
        "weight_stability": weight_stability,
        "statistical_adjudication": statistical_adjudication,
        "checks": checks,
        "report_only_checks": report_only_checks,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    initialize_stability_schema(target)
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO research_stability_runs (
                stability_id, loop_id, candidate_signature, status,
                window_count, artifact_path, artifact_sha256, summary_json,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stability_id) DO UPDATE SET
                status=excluded.status,
                window_count=excluded.window_count,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                summary_json=excluded.summary_json,
                created_at_utc=excluded.created_at_utc
            """,
            (
                stability_id,
                loop_id,
                candidate_signature,
                status,
                len(rolling_windows),
                str(written),
                payload["artifact_sha256"],
                canonical_json_bytes(
                    {
                        "status": status,
                        "checks": checks,
                        "rolling_windows": {
                            key: value
                            for key, value in rolling_summary.items()
                            if key != "windows"
                        },
                        "cost_sensitivity": {
                            key: value
                            for key, value in cost_summary.items()
                            if key != "cases"
                        },
                    },
                    trailing_newline=False,
                ).decode("utf-8"),
                _utc_now(),
            ),
        )
    return payload


__all__ = [
    "CANDIDATE_TRADE_COUNT_SCOPE",
    "DEFAULT_STABILITY_CONFIG",
    "DEFAULT_STABILITY_OUTPUT_DIR",
    "DEFAULT_STABILITY_REPORT_DIR",
    "ResearchStabilityError",
    "TRADE_COUNT_POLICY_VERSION",
    "initialize_stability_schema",
    "normalize_stability_config",
    "objective_with_fold_trade_minimum",
    "run_research_stability",
    "summarize_candidate_trade_count",
    "summarize_weight_stability",
    "trade_count_policy_identity",
]
