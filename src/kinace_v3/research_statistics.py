"""Phase 1a statistical inference machinery for research evidence."""

from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .trial_backtest import initialize_trial_backtest_schema, trial_config_hash


SCHEMA_VERSION = 1
TRADING_DAYS_PER_YEAR = 252
DEFAULT_BOOTSTRAP_ITERATIONS = 2_000
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_RANDOM_SEED = 0
DEFAULT_Z_CRITICAL = 1.96
DEFAULT_MIN_OBSERVATION_SESSIONS = 3
DEFAULT_MIN_TRADE_COUNT = 0
DEFAULT_STATISTICS_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_statistics"
DEFAULT_STATISTICS_REPORT_DIR = PROJECT_ROOT / "reports" / "research_statistics"


class ResearchStatisticsError(ValueError):
    """Raised when Phase 1a statistical inference cannot be computed safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _plain_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchStatisticsError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchStatisticsError(f"{field} must be finite")
    return result


def _optional_finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _binding_gate_summary(checks: Mapping[str, bool]) -> dict[str, Any]:
    normalized = {str(name): bool(value) for name, value in checks.items()}
    return {
        "passed": all(normalized.values()),
        "checks": normalized,
        "failed_checks": [name for name, value in normalized.items() if not value],
    }


def _normalize_returns(values: Sequence[Any]) -> list[float]:
    returns = [_plain_number(value, "return") for value in values]
    if any(value <= -1.0 for value in returns):
        raise ResearchStatisticsError("returns must be greater than -1")
    return returns


def _sample_stdev(values: Sequence[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def _annualized_sharpe(returns: Sequence[float]) -> float | None:
    stdev = _sample_stdev(returns)
    if stdev is None or stdev <= 0:
        return None
    return statistics.fmean(returns) / stdev * math.sqrt(TRADING_DAYS_PER_YEAR)


def _annualized_return(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    total_return = _compounded_return(returns)
    if total_return <= -1.0:
        return None
    return (1.0 + total_return) ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1.0


def _compounded_return(returns: Sequence[float]) -> float:
    return math.prod(1.0 + value for value in returns) - 1.0


def _autocorrelation(values: Sequence[float], lag: int) -> float:
    if lag <= 0 or lag >= len(values):
        return 0.0
    mean = statistics.fmean(values)
    denominator = sum((value - mean) ** 2 for value in values)
    if denominator <= 0:
        return 0.0
    numerator = sum(
        (values[index] - mean) * (values[index - lag] - mean)
        for index in range(lag, len(values))
    )
    return numerator / denominator


def _autocorrelation_summary(
    returns: Sequence[float],
    *,
    truncation: int | None,
) -> dict[str, Any]:
    count = len(returns)
    if count <= 1:
        return {
            "method": "newey_west_autocorrelation",
            "rho_k_truncation": 0,
            "rho_1": None,
            "rho_k": {},
            "rho_sum": 0.0,
            "variance_inflation": 1.0,
            "N_eff_measured": float(count),
            "unavailable_reason": (
                "minimum_observation_sessions_not_met" if count == 0 else None
            ),
        }
    resolved_truncation = (
        max(1, min(count - 1, int(math.floor(4.0 * (count / 100.0) ** (2.0 / 9.0)))))
        if truncation is None
        else int(truncation)
    )
    if resolved_truncation <= 0 or resolved_truncation >= count:
        raise ResearchStatisticsError(
            "autocorrelation truncation must be between 1 and T-1"
        )
    rhos = {
        str(lag): _autocorrelation(returns, lag)
        for lag in range(1, resolved_truncation + 1)
    }
    rho_sum = sum(rhos.values())
    inflation = 1.0 + 2.0 * rho_sum
    if inflation <= 1e-12:
        return {
            "method": "newey_west_autocorrelation",
            "rho_k_truncation": resolved_truncation,
            "rho_1": rhos["1"],
            "rho_k": rhos,
            "rho_sum": rho_sum,
            "variance_inflation": 1.0,
            "N_eff_measured": float(count),
            "unavailable_reason": "autocorrelation_correction_non_positive",
        }
    effective_sample_size = count / inflation
    return {
        "method": "newey_west_autocorrelation",
        "rho_k_truncation": resolved_truncation,
        "rho_1": rhos["1"],
        "rho_k": rhos,
        "rho_sum": rho_sum,
        "variance_inflation": inflation,
        "N_eff_measured": effective_sample_size,
    }


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ResearchStatisticsError("cannot compute a quantile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _bootstrap_summary(
    *,
    returns: Sequence[float],
    bootstrap_iterations: int,
    confidence_level: float,
    seed: int,
    block_size: int | None,
) -> dict[str, Any]:
    if bootstrap_iterations <= 0:
        raise ResearchStatisticsError("bootstrap_iterations must be positive")
    if not 0 < confidence_level < 1:
        raise ResearchStatisticsError("confidence_level must be in (0, 1)")
    resolved_block_size = (
        max(1, min(len(returns), int(round(len(returns) ** (1.0 / 3.0)))))
        if block_size is None
        else int(block_size)
    )
    if resolved_block_size <= 0 or resolved_block_size > len(returns):
        raise ResearchStatisticsError("block_size must be in [1, T]")
    blocks = [
        list(returns[index : index + resolved_block_size])
        for index in range(0, len(returns) - resolved_block_size + 1)
    ]
    rng = random.Random(seed)
    sharpes: list[float] = []
    total_returns: list[float] = []
    for _ in range(bootstrap_iterations):
        sample: list[float] = []
        while len(sample) < len(returns):
            sample.extend(rng.choice(blocks))
        sample = sample[: len(returns)]
        sharpe = _annualized_sharpe(sample)
        if sharpe is None:
            continue
        sharpes.append(sharpe)
        total_returns.append(_compounded_return(sample))
    if not sharpes:
        raise ResearchStatisticsError("bootstrap produced no finite Sharpe samples")
    tail = (1.0 - confidence_level) / 2.0

    def summary(values: Sequence[float]) -> dict[str, float]:
        stdev = _sample_stdev(values)
        return {
            "lower": _quantile(values, tail),
            "median": _quantile(values, 0.5),
            "upper": _quantile(values, 1.0 - tail),
            "mean": statistics.fmean(values),
            "stdev": stdev if stdev is not None else 0.0,
        }

    return {
        "method": "moving_block_bootstrap",
        "iterations": bootstrap_iterations,
        "seed": seed,
        "confidence_level": confidence_level,
        "block_size": resolved_block_size,
        "annualized_sharpe": summary(sharpes),
        "total_return": summary(total_returns),
        "computable": True,
        "unavailable_reason": None,
    }


def _unavailable_bootstrap_summary(
    *,
    bootstrap_iterations: int,
    confidence_level: float,
    seed: int,
    block_size: int | None,
    unavailable_reason: str,
) -> dict[str, Any]:
    return {
        "method": "moving_block_bootstrap",
        "iterations": bootstrap_iterations,
        "seed": seed,
        "confidence_level": confidence_level,
        "block_size": block_size,
        "annualized_sharpe": None,
        "total_return": None,
        "computable": False,
        "unavailable_reason": unavailable_reason,
    }


def _normal_cdf(value: float) -> float:
    return statistics.NormalDist().cdf(value)


def _deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    N_trials: int,
) -> dict[str, Any]:
    if N_trials <= 0:
        raise ResearchStatisticsError("N_trials must be positive")
    stdev = _sample_stdev(returns)
    if stdev is None or stdev <= 0:
        reason = "return standard deviation is zero or unavailable"
        return {
            "computable": False,
            "method": "deflated_sharpe_ratio",
            "N_trials": N_trials,
            "observed_daily_sharpe": None,
            "expected_max_sharpe_daily": None,
            "skewness": None,
            "kurtosis": None,
            "statistic": None,
            "dsr": None,
            "p_value": None,
            "passes_5pct": None,
            "reason": reason,
            "unavailable_reason": reason,
        }
    daily_sharpe = statistics.fmean(returns) / stdev
    count = len(returns)
    expected_max_sharpe = (
        math.sqrt(2.0 * math.log(N_trials) / count) if N_trials > 1 else 0.0
    )
    mean = statistics.fmean(returns)
    second_moment = statistics.fmean((value - mean) ** 2 for value in returns)
    third_moment = statistics.fmean((value - mean) ** 3 for value in returns)
    fourth_moment = statistics.fmean((value - mean) ** 4 for value in returns)
    skewness = third_moment / stdev**3
    kurtosis = fourth_moment / stdev**4
    denominator = math.sqrt(
        max(
            1e-12,
            1.0
            - skewness * daily_sharpe
            + (kurtosis - 1.0) * daily_sharpe**2 / 4.0,
        )
    )
    statistic = (
        (daily_sharpe - expected_max_sharpe)
        * math.sqrt(max(count - 1, 1))
        / denominator
    )
    confidence = _normal_cdf(statistic)
    return {
        "computable": True,
        "method": "deflated_sharpe_ratio",
        "N_trials": N_trials,
        "observed_daily_sharpe": daily_sharpe,
        "expected_max_sharpe_daily": expected_max_sharpe,
        "skewness": skewness,
        "kurtosis": kurtosis,
        "statistic": statistic,
        "dsr": confidence,
        "p_value": 1.0 - confidence,
        "passes_5pct": confidence >= 0.95,
        "unavailable_reason": None,
    }


def _target_power(
    returns: Sequence[float],
    *,
    target_sharpe: float | None,
    variance_inflation: float,
    z_critical: float,
) -> dict[str, Any]:
    measured_neff = (
        len(returns) / variance_inflation
        if math.isfinite(variance_inflation) and variance_inflation > 0
        else None
    )
    if target_sharpe is None:
        return {
            "target_sharpe": None,
            "target_sharpe_frozen": False,
            "z_critical": z_critical,
            "N_eff_required": None,
            "N_eff_measured": measured_neff,
            "N_eff_met": None,
            "required_sessions": None,
            "available_sessions": len(returns),
            "met": None,
            "deficit_sessions": None,
            "formula": "N_eff_required=(z_critical/target_sharpe)^2*252; required_sessions=ceil(N_eff_required*variance_inflation)",
        }
    target = _plain_number(target_sharpe, "target_sharpe")
    if target <= 0:
        raise ResearchStatisticsError("target_sharpe must be positive")
    if z_critical <= 0:
        raise ResearchStatisticsError("z_critical must be positive")
    required_neff = (z_critical / target) ** 2 * TRADING_DAYS_PER_YEAR
    required_sessions = int(math.ceil(required_neff * variance_inflation))
    neff_met = measured_neff is not None and measured_neff >= required_neff
    return {
        "target_sharpe": target,
        "target_sharpe_frozen": True,
        "z_critical": z_critical,
        "N_eff_required": required_neff,
        "N_eff_measured": measured_neff,
        "N_eff_met": neff_met,
        "required_sessions": required_sessions,
        "available_sessions": len(returns),
        "met": neff_met,
        "deficit_sessions": max(0, required_sessions - len(returns)),
        "formula": "N_eff_required=(z_critical/target_sharpe)^2*252; required_sessions=ceil(N_eff_required*variance_inflation)",
    }


def analyze_return_series(
    returns: Sequence[Any],
    *,
    trade_count: int | None = None,
    min_observation_sessions: int = DEFAULT_MIN_OBSERVATION_SESSIONS,
    min_trade_count: int = DEFAULT_MIN_TRADE_COUNT,
    target_sharpe: float | None = None,
    N_trials: int = 1,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    seed: int = DEFAULT_RANDOM_SEED,
    block_size: int | None = None,
    autocorrelation_truncation: int | None = None,
    z_critical: float = DEFAULT_Z_CRITICAL,
) -> dict[str, Any]:
    """Compute the reusable Phase 1a sample, bootstrap, and DSR statistics."""
    normalized = _normalize_returns(returns)
    if (
        isinstance(min_observation_sessions, bool)
        or not isinstance(min_observation_sessions, int)
        or min_observation_sessions < 0
    ):
        raise ResearchStatisticsError(
            "min_observation_sessions must be a non-negative integer"
        )
    if (
        isinstance(min_trade_count, bool)
        or not isinstance(min_trade_count, int)
        or min_trade_count < 0
    ):
        raise ResearchStatisticsError(
            "min_trade_count must be a non-negative integer"
        )
    if trade_count is not None and (
        isinstance(trade_count, bool)
        or not isinstance(trade_count, int)
        or trade_count < 0
    ):
        raise ResearchStatisticsError("trade_count must be a non-negative integer")
    if isinstance(N_trials, bool) or not isinstance(N_trials, int) or N_trials <= 0:
        raise ResearchStatisticsError("N_trials must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ResearchStatisticsError("seed must be an integer")

    unavailable_reasons: list[str] = []
    if len(normalized) < min_observation_sessions:
        unavailable_reasons.append("minimum_observation_sessions_not_met")
    if min_trade_count > 0 and (
        trade_count is None or trade_count < min_trade_count
    ):
        unavailable_reasons.append("minimum_trade_count_not_met")

    observed_sharpe = _annualized_sharpe(normalized)
    if unavailable_reasons:
        observed_sharpe = None
    elif observed_sharpe is None:
        unavailable_reasons.append("sharpe_unavailable")
    observed_stdev = _sample_stdev(normalized)
    autocorrelation = _autocorrelation_summary(
        normalized,
        truncation=autocorrelation_truncation,
    )
    if unavailable_reasons:
        bootstrap = _unavailable_bootstrap_summary(
            bootstrap_iterations=bootstrap_iterations,
            confidence_level=confidence_level,
            seed=seed,
            block_size=block_size,
            unavailable_reason=";".join(unavailable_reasons),
        )
        dsr = {
            "computable": False,
            "method": "deflated_sharpe_ratio",
            "N_trials": N_trials,
            "observed_daily_sharpe": None,
            "expected_max_sharpe_daily": None,
            "skewness": None,
            "kurtosis": None,
            "statistic": None,
            "dsr": None,
            "p_value": None,
            "passes_5pct": None,
            "reason": ";".join(unavailable_reasons),
            "unavailable_reason": ";".join(unavailable_reasons),
        }
    else:
        try:
            bootstrap = _bootstrap_summary(
                returns=normalized,
                bootstrap_iterations=bootstrap_iterations,
                confidence_level=confidence_level,
                seed=seed,
                block_size=block_size,
            )
        except ResearchStatisticsError as exc:
            unavailable_reasons.append("bootstrap_sharpe_unavailable")
            bootstrap = _unavailable_bootstrap_summary(
                bootstrap_iterations=bootstrap_iterations,
                confidence_level=confidence_level,
                seed=seed,
                block_size=block_size,
                unavailable_reason=f"bootstrap_sharpe_unavailable:{exc}",
            )
        dsr = _deflated_sharpe_ratio(normalized, N_trials=N_trials)
    target_power = _target_power(
        normalized,
        target_sharpe=target_sharpe,
        variance_inflation=float(autocorrelation["variance_inflation"]),
        z_critical=z_critical,
    )
    sample_requirements = {
        "passed": not unavailable_reasons,
        "available_sessions": len(normalized),
        "required_sessions": min_observation_sessions,
        "sessions_met": len(normalized) >= min_observation_sessions,
        "available_trades": trade_count,
        "required_trades": min_trade_count,
        "trades_met": min_trade_count == 0
        or (trade_count is not None and trade_count >= min_trade_count),
        "unavailable_reasons": unavailable_reasons,
    }
    sharpe_interval = bootstrap.get("annualized_sharpe")
    sharpe_lower = (
        _optional_finite_number(sharpe_interval.get("lower"))
        if isinstance(sharpe_interval, Mapping)
        else None
    )
    dsr_computable = bool(dsr.get("computable") is True)
    dsr_p_value = _optional_finite_number(dsr.get("p_value"))
    n_eff_measured = _optional_finite_number(
        autocorrelation.get("N_eff_measured")
    )
    binding_gates = _binding_gate_summary(
        {
            "bootstrap_confidence_interval_computable": bool(
                bootstrap.get("computable") is True and sharpe_lower is not None
            ),
            "bootstrap_confidence_interval_lower_bound_positive": bool(
                sharpe_lower is not None and sharpe_lower > 0
            ),
            "deflated_sharpe_computable": dsr_computable,
            "deflated_sharpe_p_value_below_0_05": bool(
                dsr_computable
                and dsr_p_value is not None
                and dsr_p_value < 0.05
            ),
            "effective_sample_size_meets_target": bool(
                n_eff_measured is not None
                and target_power.get("N_eff_met") is True
            ),
            "minimum_observation_sessions_established": bool(
                min_observation_sessions > 0
            ),
            "minimum_observation_sessions_met": bool(
                sample_requirements["sessions_met"]
            ),
            "minimum_trades_established": bool(min_trade_count > 0),
            "minimum_trades_met": bool(sample_requirements["trades_met"]),
        }
    )
    return {
        "status": "available" if sample_requirements["passed"] else "unavailable",
        "unavailable_reason": (
            ";".join(unavailable_reasons) if unavailable_reasons else None
        ),
        "sample_requirements": sample_requirements,
        "observed": {
            "sessions": len(normalized),
            "trades": trade_count,
            "mean_daily_return": (
                statistics.fmean(normalized) if normalized else None
            ),
            "daily_stdev": observed_stdev,
            "annualized_return": (
                _annualized_return(normalized)
                if sample_requirements["passed"]
                else None
            ),
            "annualized_sharpe": observed_sharpe,
            "total_return": _compounded_return(normalized),
        },
        "autocorrelation": autocorrelation,
        "effective_sample_size": {
            "N_eff_measured": autocorrelation["N_eff_measured"],
            "variance_inflation": autocorrelation["variance_inflation"],
        },
        "bootstrap_confidence_interval": bootstrap,
        "deflated_sharpe_ratio": dsr,
        "target_power": target_power,
        "binding_gates": binding_gates,
    }


def build_trial_ledger(
    database: str | Path,
    *,
    loop_id: str | None = None,
    unrecorded_trials_upper_bound: int | None = None,
    assume_recorded_trials_complete: bool = False,
) -> dict[str, Any]:
    """Count recorded unique trials from immutable backtest evidence."""
    if unrecorded_trials_upper_bound is not None and (
        isinstance(unrecorded_trials_upper_bound, bool)
        or not isinstance(unrecorded_trials_upper_bound, int)
        or unrecorded_trials_upper_bound < 0
    ):
        raise ResearchStatisticsError("unrecorded_trials_upper_bound must be non-negative")
    if not isinstance(assume_recorded_trials_complete, bool):
        raise ResearchStatisticsError(
            "assume_recorded_trials_complete must be a boolean"
        )
    if (
        assume_recorded_trials_complete
        and unrecorded_trials_upper_bound not in (None, 0)
    ):
        raise ResearchStatisticsError(
            "a proven-complete ledger cannot include unrecorded trials"
        )
    target = validate_database_path(database)
    with _connect(target) as connection:
        tables = _table_names(connection)
        if "backtest_runs" not in tables:
            raise ResearchStatisticsError("backtest_runs table is absent")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(backtest_runs)")
        }
        has_stored_hash = "config_hash" in columns
        select_hash = ", config_hash" if has_stored_hash else ""
        rows = connection.execute(
            "SELECT run_id, candidate_id, config_json" + select_hash + " FROM backtest_runs"
        ).fetchall()
        run_to_trial: dict[str, str] = {}
        trial_ids: set[str] = set()
        stored_hashes: set[str] = set()
        for row in rows:
            try:
                config = json.loads(row["config_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ResearchStatisticsError(
                    f"backtest run has invalid config_json: {row['run_id']}"
                ) from exc
            if not isinstance(config, Mapping):
                raise ResearchStatisticsError(
                    f"backtest run config_json must be an object: {row['run_id']}"
                )
            stored_hash = str(row["config_hash"] or "") if has_stored_hash else ""
            trial_id = stored_hash or trial_config_hash(str(row["candidate_id"]), config)
            run_to_trial[str(row["run_id"])] = trial_id
            trial_ids.add(trial_id)
            if stored_hash:
                stored_hashes.add(stored_hash)

        loop_trial_ids: set[str] = set()
        if loop_id is not None and "research_evidence_runs" in tables:
            run_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT run_id FROM research_evidence_runs WHERE loop_id=?",
                    (str(loop_id),),
                )
            ]
            loop_trial_ids = {
                run_to_trial[run_id] for run_id in run_ids if run_id in run_to_trial
            }

        discarded_trial_ids: set[str] = set()
        if {
            "research_evidence_runs",
            "research_candidates",
        } <= tables:
            discarded_run_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT evidence.run_id
                    FROM research_evidence_runs AS evidence
                    JOIN research_candidates AS candidate
                      ON candidate.candidate_signature = evidence.candidate_signature
                    WHERE candidate.stage IN ('rejected', 'superseded')
                       OR candidate.holdout_status = 'holdout_rejected'
                       OR candidate.superseded_by_loop_id IS NOT NULL
                    """
                )
            ]
            discarded_trial_ids = {
                run_to_trial[run_id]
                for run_id in discarded_run_ids
                if run_id in run_to_trial
            }

    recorded_count = len(trial_ids)
    resolved_upper_bound = unrecorded_trials_upper_bound or 0
    total_count = recorded_count + resolved_upper_bound
    ledger_accounting_complete = bool(
        assume_recorded_trials_complete
        or unrecorded_trials_upper_bound is not None
    )
    if assume_recorded_trials_complete:
        ledger_basis = "recorded_evidence_assumed_complete"
    elif unrecorded_trials_upper_bound is not None:
        ledger_basis = "recorded_evidence_plus_frozen_unrecorded_upper_bound"
    else:
        ledger_basis = "recorded_evidence_lower_bound_only"
    identity = {
        "database": str(target),
        "loop_id": loop_id,
        "recorded_trial_ids": sorted(trial_ids),
        "unrecorded_trials_upper_bound": unrecorded_trials_upper_bound,
        "assume_recorded_trials_complete": assume_recorded_trials_complete,
    }
    return {
        "artifact_type": "kinace_v3_trial_ledger",
        "schema_version": SCHEMA_VERSION,
        "database": str(target),
        "loop_id": loop_id,
        "N_trials_total": total_count,
        "N_trials_total_recorded": recorded_count,
        "N_trials_this_loop": len(loop_trial_ids) if loop_id is not None else None,
        "N_trials_discarded": len(discarded_trial_ids),
        "unrecorded_trials_upper_bound": unrecorded_trials_upper_bound,
        "N_trials_total_lower_bound": recorded_count,
        "N_trials_total_upper_bound": (
            total_count if ledger_accounting_complete else None
        ),
        "ledger_accounting_complete": ledger_accounting_complete,
        "ledger_basis": ledger_basis,
        "ledger_complete": assume_recorded_trials_complete,
        "recorded_trial_ids": sorted(trial_ids),
        "loop_trial_ids": sorted(loop_trial_ids),
        "discarded_trial_ids": sorted(discarded_trial_ids),
        "sources": {
            "backtest_runs": len(trial_ids),
            "distinct_stored_config_hash": len(stored_hashes),
            "loop_evidence_run_count": len(loop_trial_ids),
            "discarded_trial_count": len(discarded_trial_ids),
        },
        "ledger_hash": sha256_bytes(
            canonical_json_bytes(identity, trailing_newline=False)
        ),
    }


def load_backtest_returns(database: str | Path, run_id: str) -> dict[str, Any]:
    """Load a persisted backtest equity curve and convert it to daily returns."""
    target = validate_database_path(database)
    with _connect(target) as connection:
        row = connection.execute(
            """
            SELECT run_id, experiment_id, candidate_id, start_date, end_date,
                   config_hash, config_json, metrics_json, equity_json
            FROM backtest_runs WHERE run_id=?
            """,
            (str(run_id),),
        ).fetchone()
    if row is None:
        raise ResearchStatisticsError(f"backtest run is not recorded: {run_id}")
    try:
        equity = json.loads(row["equity_json"])
        metrics = json.loads(row["metrics_json"])
        config = json.loads(row["config_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResearchStatisticsError("backtest run contains invalid JSON") from exc
    if not isinstance(equity, list) or not isinstance(metrics, Mapping):
        raise ResearchStatisticsError("backtest equity and metrics must be structured data")
    points: list[tuple[str, float]] = []
    for item in equity:
        if not isinstance(item, Mapping):
            continue
        trade_date = str(item.get("trade_date") or "")
        value = item.get("total_assets")
        if value is None and item.get("cash") is not None:
            value = float(item.get("cash") or 0.0) + float(item.get("market_value") or 0.0)
        if not trade_date or value is None:
            continue
        points.append((trade_date, _plain_number(value, "total_assets")))
    points.sort(key=lambda item: item[0])
    if not points:
        raise ResearchStatisticsError("backtest equity curve is empty")
    values = [value for _, value in points]
    if any(value <= 0 for value in values):
        raise ResearchStatisticsError("backtest equity values must be positive")
    returns = [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))]
    return {
        "run_id": str(row["run_id"]),
        "experiment_id": str(row["experiment_id"]),
        "candidate_id": str(row["candidate_id"]),
        "start_date": str(row["start_date"] or ""),
        "end_date": str(row["end_date"] or ""),
        "config_hash": str(row["config_hash"] or ""),
        "config": dict(config),
        "metrics": dict(metrics),
        "returns": returns,
        "returns_hash": sha256_bytes(
            canonical_json_bytes(returns, trailing_newline=False)
        ),
    }


def initialize_statistics_schema(database: str | Path | None = None) -> Path:
    target = initialize_trial_backtest_schema(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_statistics_runs (
                statistics_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                loop_id TEXT,
                status TEXT NOT NULL,
                target_sharpe REAL,
                N_trials_total INTEGER NOT NULL,
                N_eff_measured REAL NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                summary_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_research_statistics_run
                ON research_statistics_runs(run_id, created_at_utc);
            """
        )
    return target


def _markdown_report(payload: Mapping[str, Any]) -> str:
    observed = payload["statistics"]["observed"]
    autocorrelation = payload["statistics"]["autocorrelation"]
    bootstrap = payload["statistics"]["bootstrap_confidence_interval"]
    dsr = payload["statistics"]["deflated_sharpe_ratio"]
    target = payload["statistics"]["target_power"]
    ledger = payload["trial_ledger"]
    sharpe_interval = bootstrap.get("annualized_sharpe")
    lines = [
        "# Kinace Phase 1a Statistical Machinery",
        "",
        f"- Statistics ID: `{payload['statistics_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Run: `{payload['run_id']}`",
        f"- Sessions: `{observed['sessions']}`",
        f"- Trades: `{observed.get('trades')}`",
        f"- Raw cumulative return: `{observed['total_return']}`",
        f"- Sample requirements met: `{payload['statistics']['sample_requirements']['passed']}`",
        f"- Unavailable reason: `{payload['statistics'].get('unavailable_reason')}`",
        f"- N_trials_total: `{ledger['N_trials_total']}` recorded `{ledger['N_trials_total_recorded']}`",
        f"- Trial ledger complete: `{ledger['ledger_complete']}`",
        f"- Trial ledger accounting complete: `{ledger['ledger_accounting_complete']}`",
        f"- N_eff: `{autocorrelation['N_eff_measured']}`",
        f"- rho_1: `{autocorrelation['rho_1']}`",
        f"- Annualized return: `{observed.get('annualized_return')}`",
        f"- Annualized Sharpe: `{observed.get('annualized_sharpe')}`",
        f"- Annualized Sharpe CI: `{sharpe_interval.get('lower') if sharpe_interval else None}` to `{sharpe_interval.get('upper') if sharpe_interval else None}`",
        f"- DSR: `{dsr.get('dsr')}` p-value `{dsr.get('p_value')}`",
        f"- Binding gates passed: `{payload['statistics']['binding_gates']['passed']}`",
        f"- Failed binding checks: `{payload['statistics']['binding_gates']['failed_checks']}`",
        f"- target_sharpe: `{target['target_sharpe']}`",
        f"- Required sessions at target: `{target['required_sessions']}`",
        "",
        "## Scope",
        "",
        "- Phase 1a provides reusable statistical inference machinery.",
        "- It does not issue a candidate promotion verdict; Phase 1b owns that provisional decision.",
        "- A frozen target Sharpe is required before Phase 1b can finalize its power threshold.",
        "",
    ]
    return "\n".join(lines)


def run_research_statistics(
    database: str | Path | None,
    run_id: str,
    *,
    loop_id: str | None = None,
    min_observation_sessions: int = DEFAULT_MIN_OBSERVATION_SESSIONS,
    min_trade_count: int = DEFAULT_MIN_TRADE_COUNT,
    target_sharpe: float | None = None,
    unrecorded_trials_upper_bound: int | None = None,
    assume_recorded_trials_complete: bool = False,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    seed: int = DEFAULT_RANDOM_SEED,
    block_size: int | None = None,
    autocorrelation_truncation: int | None = None,
    z_critical: float = DEFAULT_Z_CRITICAL,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_STATISTICS_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_STATISTICS_REPORT_DIR,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    source = load_backtest_returns(target, run_id)
    ledger = build_trial_ledger(
        target,
        loop_id=loop_id,
        unrecorded_trials_upper_bound=unrecorded_trials_upper_bound,
        assume_recorded_trials_complete=assume_recorded_trials_complete,
    )
    raw_closed_stats = source["metrics"].get("closed_trade_stats")
    closed_stats = (
        raw_closed_stats if isinstance(raw_closed_stats, Mapping) else source["metrics"]
    )
    raw_trade_count = closed_stats.get("trade_count")
    trade_count = (
        int(raw_trade_count)
        if isinstance(raw_trade_count, int) and not isinstance(raw_trade_count, bool)
        else None
    )
    statistics_payload = analyze_return_series(
        source["returns"],
        trade_count=trade_count,
        min_observation_sessions=min_observation_sessions,
        min_trade_count=min_trade_count,
        target_sharpe=target_sharpe,
        N_trials=int(ledger["N_trials_total"]),
        bootstrap_iterations=bootstrap_iterations,
        confidence_level=confidence_level,
        seed=seed,
        block_size=block_size,
        autocorrelation_truncation=autocorrelation_truncation,
        z_critical=z_critical,
    )
    binding_checks = {
        **statistics_payload["binding_gates"]["checks"],
        "trial_ledger_accounting_complete": bool(
            ledger["ledger_accounting_complete"]
        ),
        "N_trials_available": int(ledger["N_trials_total"]) > 0,
    }
    statistics_payload["binding_gates"] = _binding_gate_summary(binding_checks)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "loop_id": loop_id,
        "returns_hash": source["returns_hash"],
        "ledger_hash": ledger["ledger_hash"],
        "min_observation_sessions": int(min_observation_sessions),
        "min_trade_count": int(min_trade_count),
        "target_sharpe": target_sharpe,
        "bootstrap_iterations": int(bootstrap_iterations),
        "confidence_level": float(confidence_level),
        "seed": int(seed),
        "block_size": block_size,
        "autocorrelation_truncation": autocorrelation_truncation,
        "z_critical": float(z_critical),
    }
    statistics_id = sha256_bytes(
        canonical_json_bytes(identity, trailing_newline=False)
    )
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False)
        / f"research-statistics-{statistics_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False)
        / f"research-statistics-{statistics_id}.md"
    )
    payload = {
        "artifact_type": "kinace_v3_research_statistics",
        "schema_version": SCHEMA_VERSION,
        "statistics_id": statistics_id,
        "status": (
            "ready"
            if statistics_payload["binding_gates"]["passed"]
            and statistics_payload["target_power"]["target_sharpe_frozen"]
            else "unavailable"
        ),
        "phase": "1a",
        "decision_status": "not_issued_phase_1b_required",
        "run_id": source["run_id"],
        "experiment_id": source["experiment_id"],
        "candidate_id": source["candidate_id"],
        "loop_id": loop_id,
        "database": str(target),
        "date_range": {
            "start": source["start_date"],
            "end": source["end_date"],
        },
        "input": {
            "returns_hash": source["returns_hash"],
            "config_hash": source["config_hash"],
            "source_metrics": source["metrics"],
        },
        "settings": {
            "bootstrap_iterations": int(bootstrap_iterations),
            "confidence_level": float(confidence_level),
            "seed": int(seed),
            "block_size": block_size,
            "autocorrelation_truncation": autocorrelation_truncation,
            "z_critical": float(z_critical),
            "min_observation_sessions": int(min_observation_sessions),
            "min_trade_count": int(min_trade_count),
        },
        "trial_ledger": ledger,
        "statistics": statistics_payload,
        "phase_1b_ready": bool(
            statistics_payload["binding_gates"]["passed"]
            and statistics_payload["target_power"]["target_sharpe_frozen"]
        ),
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    initialize_statistics_schema(target)
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO research_statistics_runs (
                statistics_id, run_id, loop_id, status, target_sharpe,
                N_trials_total, N_eff_measured, artifact_path, artifact_sha256,
                summary_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(statistics_id) DO UPDATE SET
                status=excluded.status,
                target_sharpe=excluded.target_sharpe,
                N_trials_total=excluded.N_trials_total,
                N_eff_measured=excluded.N_eff_measured,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                summary_json=excluded.summary_json,
                created_at_utc=excluded.created_at_utc
            """,
            (
                statistics_id,
                source["run_id"],
                loop_id,
                payload["status"],
                target_sharpe,
                int(ledger["N_trials_total"]),
                float(statistics_payload["effective_sample_size"]["N_eff_measured"]),
                str(written),
                payload["artifact_sha256"],
                canonical_json_bytes(
                    {
                        "decision_status": payload["decision_status"],
                        "phase_1b_ready": payload["phase_1b_ready"],
                        "N_trials_total": ledger["N_trials_total"],
                        "N_eff_measured": statistics_payload["effective_sample_size"][
                            "N_eff_measured"
                        ],
                        "annualized_sharpe": statistics_payload["observed"][
                            "annualized_sharpe"
                        ],
                        "dsr": statistics_payload["deflated_sharpe_ratio"].get("dsr"),
                    },
                    trailing_newline=False,
                ).decode("utf-8"),
                _utc_now(),
            ),
        )
    return payload


__all__ = [
    "DEFAULT_BOOTSTRAP_ITERATIONS",
    "DEFAULT_CONFIDENCE_LEVEL",
    "DEFAULT_MIN_OBSERVATION_SESSIONS",
    "DEFAULT_MIN_TRADE_COUNT",
    "DEFAULT_RANDOM_SEED",
    "DEFAULT_STATISTICS_OUTPUT_DIR",
    "DEFAULT_STATISTICS_REPORT_DIR",
    "DEFAULT_Z_CRITICAL",
    "ResearchStatisticsError",
    "SCHEMA_VERSION",
    "analyze_return_series",
    "build_trial_ledger",
    "initialize_statistics_schema",
    "load_backtest_returns",
    "run_research_statistics",
]
