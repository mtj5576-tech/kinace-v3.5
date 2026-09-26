"""Automated iterative research loop with walk-forward validation and holdout."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import (
    FEATURE_NAMES,
    canonical_json_bytes,
    load_feature_snapshot_artifact,
    sha256_bytes,
    sha256_file,
)
from .holdout_access import (
    HoldoutAccessError,
    SANDBOX_REGISTRY_FILENAME,
    open_holdout_access,
    resolve_holdout_access_registry_path,
)
from .optimization import (
    DEFAULT_OPTIMIZATION_OUTPUT_DIR,
    normalize_objective_config,
    normalize_optimization_payload,
    score_portfolio_objective,
)
from .portfolio_backtest import (
    discover_snapshot_dates,
    discover_snapshots,
    load_market_bars,
    run_portfolio_backtest,
)
from .research_protocol import (
    archive_active_challengers,
    build_candidate_config_manifest,
    build_effective_research_protocol,
    protocol_candidate_id,
    write_candidate_manifest,
    write_effective_protocol,
)
from .research_holdout import (
    build_holdout_extension_freeze,
    normalize_holdout_extension_policy,
    objective_without_trade_count,
    register_holdout_extension_freeze,
    write_holdout_extension_freeze,
)
from .score_replay import validate_candidate_config
from .research_stability import (
    normalize_stability_config,
    objective_with_fold_trade_minimum,
    summarize_candidate_trade_count,
)
from .research_feature_transform import (
    FeatureTransformState,
    apply_raw_input_overrides,
    normalize_feature_transform,
)
from .research_weighting import (
    fit_signed_icir_weights,
    normalize_standardization_config,
    standardize_values,
    summarize_ic_series,
)
from .trial_backtest import (
    initialize_trial_backtest_schema,
    normalize_backtest_config,
    normalize_market_bars,
)
from .research_labeling import label_value, normalize_label_horizon


SCHEMA_VERSION = 1
MAX_RESEARCH_ROUNDS = 10
MAX_CANDIDATES_PER_ROUND = 16
DEFAULT_RESEARCH_LOOP_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_loop"
DEFAULT_RESEARCH_LOOP_REPORT_DIR = PROJECT_ROOT / "reports" / "research_loop"
DEFAULT_WEIGHT_AUDIT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_weighting"
DEFAULT_WEIGHT_AUDIT_REPORT_DIR = PROJECT_ROOT / "reports" / "research_weighting"
_RANK_IC_CACHE: dict[
    tuple[str, str, str, int, float, str],
    tuple[dict[str, dict[str, Any]], list[str]],
] = {}
_SNAPSHOT_RANK_IC_CACHE: dict[tuple[str, int, str], dict[str, float] | None] = {}
DEFAULT_WALK_FORWARD: dict[str, Any] = {
    "folds": 8,
    "validation_sessions": 20,
    "test_sessions": 60,
    "min_train_sessions": 120,
    "purge_sessions": 5,
    "embargo_sessions": 5,
    "training_mode": "seed",
    "label_horizon": 5,
    "max_rounds": 2,
    "candidates_per_round": 4,
}


class ResearchLoopError(ValueError):
    """Raised when an iterative research loop cannot run safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchLoopError(f"{field} must be an object")
    return value


def _standardization_cache_key(config: Mapping[str, Any] | None) -> str:
    normalized = normalize_standardization_config(config)
    return sha256_bytes(
        canonical_json_bytes(normalized, trailing_newline=False)
    )


def _feature_transform_cache_key(config: Mapping[str, Any] | None) -> str:
    normalized = normalize_feature_transform(config)
    return sha256_bytes(
        canonical_json_bytes(normalized, trailing_newline=False)
    )


def _training_recency_settings(
    base_model_config: Mapping[str, Any],
) -> tuple[float | None, str, float]:
    source = base_model_config.get("training_policy")
    if source is None:
        return None, "direction_and_strength", 0.25
    policy = dict(_mapping(source, "model_config.training_policy"))
    unknown = sorted(
        set(policy) - {"recency_halflife_sessions", "recency_mode", "conflict_scale"}
    )
    if unknown:
        raise ResearchLoopError(
            "unknown training_policy keys: " + ", ".join(unknown)
        )
    recency_mode = str(policy.get("recency_mode") or "direction_and_strength")
    if recency_mode not in {
        "direction_and_strength",
        "direction_only",
        "conflict_contract",
    }:
        raise ResearchLoopError(
            "training_policy.recency_mode must be direction_and_strength, direction_only, or conflict_contract"
        )
    conflict_scale = policy.get("conflict_scale", 0.25)
    if isinstance(conflict_scale, bool) or not isinstance(conflict_scale, (int, float)):
        raise ResearchLoopError("training_policy.conflict_scale must be in [0, 1]")
    conflict_scale = float(conflict_scale)
    if not 0 <= conflict_scale <= 1:
        raise ResearchLoopError("training_policy.conflict_scale must be in [0, 1]")
    value = policy.get("recency_halflife_sessions")
    if value is None:
        return None, recency_mode, conflict_scale
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchLoopError(
            "training_policy.recency_halflife_sessions must be a positive number"
        )
    half_life = float(value)
    if not math.isfinite(half_life) or half_life <= 0:
        raise ResearchLoopError(
            "training_policy.recency_halflife_sessions must be a positive number"
        )
    return half_life, recency_mode, conflict_scale


def _positive_int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ResearchLoopError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ResearchLoopError(f"{field} must be <= {maximum}")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResearchLoopError(f"{field} must be a non-negative integer")
    return value


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


def _candidate_signature(
    model_config: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "model_config": model_config,
                "backtest_config": backtest_config,
            },
            trailing_newline=False,
        )
    )


def normalize_walk_forward_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(config or {})
    unknown = sorted(set(source) - set(DEFAULT_WALK_FORWARD))
    if unknown:
        raise ResearchLoopError("unknown walk_forward keys: " + ", ".join(unknown))
    result = dict(DEFAULT_WALK_FORWARD)
    result.update(source)
    training_mode = str(result["training_mode"]).strip().lower()
    if training_mode not in {"seed", "rank_ic"}:
        raise ResearchLoopError("walk_forward.training_mode must be seed or rank_ic")
    label_horizon = normalize_label_horizon(result["label_horizon"])
    return {
        "folds": _positive_int(result["folds"], "walk_forward.folds", maximum=8),
        "validation_sessions": _positive_int(
            result["validation_sessions"], "walk_forward.validation_sessions"
        ),
        "test_sessions": _positive_int(
            result["test_sessions"], "walk_forward.test_sessions"
        ),
        "min_train_sessions": _positive_int(
            result["min_train_sessions"], "walk_forward.min_train_sessions"
        ),
        "purge_sessions": _nonnegative_int(
            result["purge_sessions"], "walk_forward.purge_sessions"
        ),
        "embargo_sessions": _nonnegative_int(
            result["embargo_sessions"], "walk_forward.embargo_sessions"
        ),
        "training_mode": training_mode,
        "label_horizon": label_horizon,
        "max_rounds": _positive_int(
            result["max_rounds"],
            "walk_forward.max_rounds",
            maximum=MAX_RESEARCH_ROUNDS,
        ),
        "candidates_per_round": _positive_int(
            result["candidates_per_round"],
            "walk_forward.candidates_per_round",
            maximum=MAX_CANDIDATES_PER_ROUND,
        ),
    }


def build_walk_forward_windows(
    snapshot_dates: Sequence[str],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    settings = normalize_walk_forward_config(config)
    dates = [str(value) for value in snapshot_dates]
    purge = settings["purge_sessions"]
    embargo = settings["embargo_sessions"]
    required = (
        settings["min_train_sessions"]
        + purge
        + embargo
        + settings["folds"] * settings["validation_sessions"]
        + purge
        + embargo
        + settings["test_sessions"]
    )
    if len(dates) < required:
        raise ResearchLoopError(
            f"walk-forward requires {required} sessions, found {len(dates)}"
        )
    test_start = len(dates) - settings["test_sessions"]
    development_end = test_start - purge - embargo
    if development_end <= 0:
        raise ResearchLoopError("walk-forward has no development window before holdout")
    windows: list[dict[str, Any]] = []
    for fold_index in range(settings["folds"]):
        fold_end = development_end - (settings["folds"] - 1 - fold_index) * settings[
            "validation_sessions"
        ]
        fold_start = fold_end - settings["validation_sessions"]
        train_start = 0
        train_end = fold_start - purge - embargo
        if train_end < settings["min_train_sessions"]:
            raise ResearchLoopError(
                f"fold {fold_index} has only {train_end} training sessions"
            )
        windows.append(
            {
                "fold": fold_index,
                "train_start": dates[train_start],
                "train_end": dates[train_end - 1],
                "validation_start": dates[fold_start],
                "validation_end": dates[fold_end - 1],
            }
        )
    return {
        "settings": settings,
        "folds": windows,
        "holdout": {
            "train_start": dates[0],
            "train_end": dates[development_end - 1],
            "start": dates[test_start],
            "end": dates[-1],
            "sessions": settings["test_sessions"],
        },
    }


def _get_dotted(payload: Mapping[str, Any], dotted_key: str) -> Any:
    current: Any = payload
    for part in dotted_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _set_dotted(payload: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = [part for part in dotted_key.split(".") if part]
    current = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = deepcopy(value)


def _parameter_neighbors(
    *,
    model_config: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
    model_space: Mapping[str, Any],
    backtest_space: Mapping[str, Any],
) -> list[dict[str, Any]]:
    entries: list[tuple[float, str, str, dict[str, Any], dict[str, Any]]] = []
    for space_name, base, space in (
        ("model", model_config, model_space),
        ("backtest", backtest_config, backtest_space),
    ):
        for key in sorted(str(value) for value in space):
            options = space[key]
            if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
                raise ResearchLoopError(f"{space_name}_search_space.{key} must be a list")
            current = _get_dotted(base, key)
            for option in options:
                if option == current:
                    continue
                if isinstance(current, (int, float)) and isinstance(option, (int, float)):
                    distance = abs(float(option) - float(current))
                else:
                    distance = 1.0
                model_variant = deepcopy(dict(model_config))
                backtest_variant = deepcopy(dict(backtest_config))
                if space_name == "model":
                    _set_dotted(model_variant, key, option)
                else:
                    _set_dotted(backtest_variant, key, option)
                entries.append(
                    (distance, space_name, key, model_variant, backtest_variant)
                )
    entries.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            canonical_json_bytes(item[3], trailing_newline=False),
            canonical_json_bytes(item[4], trailing_newline=False),
        )
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, _, model_variant, backtest_variant in entries:
        model_config_value = validate_candidate_config(model_variant)
        backtest_config_value = normalize_backtest_config(backtest_variant)
        signature = _candidate_signature(model_config_value, backtest_config_value)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(
            {
                "signature": signature,
                "model_config": model_config_value,
                "backtest_config": backtest_config_value,
            }
        )
    return result


def _connect(database: str | Path) -> sqlite3.Connection:
    target = validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    definitions: Mapping[str, str],
) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'
            )


def initialize_research_loop_schema(database: str | Path | None = None) -> Path:
    target = initialize_trial_backtest_schema(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_loop_runs (
                loop_id TEXT PRIMARY KEY,
                code_version TEXT NOT NULL,
                status TEXT NOT NULL,
                fold_count INTEGER NOT NULL,
                round_count INTEGER NOT NULL,
                candidate_count INTEGER NOT NULL,
                best_candidate_signature TEXT,
                holdout_run_id TEXT,
                holdout_status TEXT,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                evaluation_protocol_hash TEXT,
                protocol_artifact_path TEXT,
                protocol_artifact_sha256 TEXT,
                candidate_manifest_path TEXT,
                candidate_manifest_sha256 TEXT,
                superseded_candidate_count INTEGER NOT NULL DEFAULT 0,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_candidates (
                candidate_signature TEXT PRIMARY KEY,
                protocol_candidate_id TEXT,
                evaluation_protocol_hash TEXT,
                code_version TEXT NOT NULL,
                model_config_json TEXT NOT NULL,
                backtest_config_json TEXT NOT NULL,
                stage TEXT NOT NULL,
                latest_loop_id TEXT NOT NULL,
                best_objective_score REAL,
                holdout_status TEXT,
                holdout_metrics_json TEXT NOT NULL,
                superseded_by_loop_id TEXT,
                superseded_reason TEXT,
                superseded_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_candidate_rounds (
                loop_id TEXT NOT NULL REFERENCES research_loop_runs(loop_id) ON DELETE CASCADE,
                round_number INTEGER NOT NULL,
                candidate_signature TEXT NOT NULL,
                status TEXT NOT NULL,
                mean_score REAL,
                min_score REAL,
                pass_rate REAL NOT NULL,
                fold_results_json TEXT NOT NULL,
                PRIMARY KEY (loop_id, round_number, candidate_signature)
            );

            CREATE TABLE IF NOT EXISTS research_evidence_runs (
                loop_id TEXT NOT NULL,
                candidate_signature TEXT NOT NULL,
                round_number INTEGER NOT NULL,
                run_kind TEXT NOT NULL,
                fold_number INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                PRIMARY KEY (loop_id, candidate_signature, run_kind, fold_number)
            );

            CREATE INDEX IF NOT EXISTS idx_research_evidence_runs_run
                ON research_evidence_runs(run_id);

            CREATE INDEX IF NOT EXISTS idx_research_candidates_stage
                ON research_candidates(stage, updated_at_utc);
            """
        )
        _ensure_columns(
            connection,
            "research_loop_runs",
            {
                "evaluation_protocol_hash": "TEXT",
                "protocol_artifact_path": "TEXT",
                "protocol_artifact_sha256": "TEXT",
                "candidate_manifest_path": "TEXT",
                "candidate_manifest_sha256": "TEXT",
                "superseded_candidate_count": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _ensure_columns(
            connection,
            "research_candidates",
            {
                "superseded_by_loop_id": "TEXT",
                "superseded_reason": "TEXT",
                "superseded_at_utc": "TEXT",
                "protocol_candidate_id": "TEXT",
                "evaluation_protocol_hash": "TEXT",
            },
        )
    return target


def _loop_id_for(
    *,
    code_version: str,
    seeds: Sequence[Mapping[str, Any]],
    objective: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
    holdout_policy: Mapping[str, Any],
    model_space: Mapping[str, Any],
    backtest_space: Mapping[str, Any],
    snapshot_dir: str | Path,
    start_date: str | None,
    end_date: str | None,
) -> str:
    identity = {
        "code_version": code_version,
        "seeds": [
            {
                "model_config": item["model_config"],
                "backtest_config": item["backtest_config"],
            }
            for item in seeds
        ],
        "objective": objective,
        "walk_forward": walk_forward,
        "holdout_policy": holdout_policy,
        "model_search_space": model_space,
        "backtest_search_space": backtest_space,
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "start_date": start_date,
        "end_date": end_date,
    }
    return sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))


def _aggregate_fold_results(
    fold_results: Sequence[Mapping[str, Any]],
    *,
    trade_count_policy: Mapping[str, Any],
) -> dict[str, Any]:
    scores = [
        float(item["objective_score"])
        for item in fold_results
        if item.get("objective_score") is not None
    ]
    returns = [
        float(item["metrics"].get("total_return") or 0.0)
        for item in fold_results
        if item.get("metrics")
    ]
    drawdowns = [
        float(item["metrics"].get("max_drawdown") or 0.0)
        for item in fold_results
        if item.get("metrics")
    ]
    passed = sum(item.get("status") == "passed" for item in fold_results)
    unavailable = sum(item.get("status") == "unavailable" for item in fold_results)
    trade_count_adjudication = summarize_candidate_trade_count(
        [
            int((item.get("metrics") or {}).get("trade_count") or 0)
            for item in fold_results
        ],
        trade_count_policy,
    )
    aggregate = {
        "status": (
            "unavailable"
            if unavailable
            else (
                "validated"
                if passed == len(fold_results) and trade_count_adjudication["passed"]
                else "rejected"
            )
        ),
        "unavailable_fold_count": unavailable,
        "unavailable_reason": next(
            (
                str(item.get("unavailable_reason"))
                for item in fold_results
                if item.get("status") == "unavailable"
                and item.get("unavailable_reason")
            ),
            None,
        ),
        "pass_rate": round(passed / len(fold_results), 6) if fold_results else 0.0,
        "mean_score": round(statistics.fmean(scores), 8) if scores else None,
        "min_score": round(min(scores), 8) if scores else None,
        "score_stdev": (
            round(statistics.stdev(scores), 8) if len(scores) >= 2 else 0.0
        ),
        "mean_total_return": round(statistics.fmean(returns), 8) if returns else 0.0,
        "mean_max_drawdown": round(statistics.fmean(drawdowns), 8)
        if drawdowns
        else 0.0,
        "trade_count_adjudication": trade_count_adjudication,
    }
    return aggregate


def _candidate_rank_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    aggregate = candidate["validation"]
    return (
        aggregate["status"] == "validated",
        aggregate["status"] != "unavailable",
        aggregate["pass_rate"],
        aggregate["mean_score"] is not None,
        aggregate["mean_score"] if aggregate["mean_score"] is not None else float("-inf"),
        aggregate["min_score"] if aggregate["min_score"] is not None else float("-inf"),
        -float(aggregate["score_stdev"] or 0.0),
    )


def _average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = (index + end - 1) / 2.0 + 1.0
        for position in range(index, end):
            ranks[ordered[position][0]] = average
        index = end
    return ranks


def _rank_ic(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 3 or len(left) != len(right):
        return 0.0
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = statistics.fmean(left_ranks)
    right_mean = statistics.fmean(right_ranks)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_ranks, right_ranks)
    )
    left_var = sum((value - left_mean) ** 2 for value in left_ranks)
    right_var = sum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_var * right_var)
    return numerator / denominator if denominator > 0 else 0.0


def _trained_weights_from_statistics(
    *,
    base_model_config: Mapping[str, Any],
    ic_statistics: Mapping[str, Mapping[str, Any]],
    start_date: str,
    end_date: str,
    used_dates: Sequence[str],
    label_horizon: int,
    max_feature_weight: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base_weights = {
        name: float(base_model_config["weights"][name]) for name in FEATURE_NAMES
    }
    fitted = fit_signed_icir_weights(
        base_weights=base_weights,
        ic_statistics=ic_statistics,
        max_feature_weight=max_feature_weight,
    )
    trained = deepcopy(dict(base_model_config))
    trained["weights"] = fitted["trained_weights"]
    trained["feature_directions"] = fitted["feature_directions"]
    trained["standardization"] = normalize_standardization_config(
        base_model_config.get("standardization")
    )
    summary = {
        "mode": "signed_icir_projected",
        "weighting_version": fitted["version"],
        "train_start": start_date,
        "train_end": end_date,
        "dates_used": len(used_dates),
        "date_list": list(used_dates),
        "used_dates": list(used_dates),
        "mean_rank_ic": {
            name: values["mean_ic"] for name, values in ic_statistics.items()
        },
        "icir": {name: values["icir"] for name, values in ic_statistics.items()},
        "newey_west_t_stat": {
            name: values["newey_west_t_stat"]
            for name, values in ic_statistics.items()
        },
        "sign_consistency": {
            name: values["sign_consistency"] for name, values in ic_statistics.items()
        },
        "ic_statistics": deepcopy(dict(ic_statistics)),
        "label_horizon": label_horizon,
        "fallback_to_base_weights": fitted["fallback_to_base_weights"],
        "feature_directions": fitted["feature_directions"],
        "raw_weights": fitted["raw_weights"],
        "trained_weights": fitted["trained_weights"],
        "max_feature_weight_budget": fitted["max_feature_weight_budget"],
        "max_feature_weight": fitted["max_feature_weight"],
        "max_feature_weight_satisfied": fitted["max_feature_weight_satisfied"],
    }
    return trained, summary


def _summarize_weight_training(
    fold_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    directions: dict[str, list[int]] = {name: [] for name in FEATURE_NAMES}
    maxima: list[float] = []
    max_weight_checks: list[bool] = []
    for fold in fold_results:
        training = fold.get("training")
        if not isinstance(training, Mapping):
            continue
        feature_directions = training.get("feature_directions")
        if isinstance(feature_directions, Mapping):
            for name in FEATURE_NAMES:
                value = feature_directions.get(name)
                if value in {-1, 1}:
                    directions[name].append(int(value))
        maximum = training.get("max_feature_weight")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
            maxima.append(float(maximum))
        satisfied = training.get("max_feature_weight_satisfied")
        if isinstance(satisfied, bool):
            max_weight_checks.append(satisfied)
    consistency = {}
    for name, values in directions.items():
        if not values:
            consistency[name] = 0.0
            continue
        positive = sum(value > 0 for value in values)
        consistency[name] = max(positive, len(values) - positive) / len(values)
    return {
        "fold_count": len(fold_results),
        "cross_window_direction_consistency": consistency,
        "minimum_cross_window_direction_consistency": min(consistency.values())
        if consistency
        else 0.0,
        "max_feature_weight": max(maxima) if maxima else None,
        "all_windows_satisfy_max_feature_weight": all(max_weight_checks)
        if max_weight_checks
        else False,
    }


def _fit_candidate_weights(
    *,
    base_model_config: Mapping[str, Any],
    snapshot_dir: str | Path,
    start_date: str,
    end_date: str,
    label_horizon: int = 5,
    max_feature_weight: float = 0.75,
) -> tuple[dict[str, Any], dict[str, Any]]:
    standardization = normalize_standardization_config(
        base_model_config.get("standardization")
    )
    recency_halflife, recency_mode, conflict_scale = _training_recency_settings(
        base_model_config
    )
    feature_transform = normalize_feature_transform(
        base_model_config.get("feature_transform")
    )
    transform_state = FeatureTransformState(feature_transform)
    transform_key = _feature_transform_cache_key(feature_transform)
    snapshots = discover_snapshots(
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    feature_ics: dict[str, list[float]] = {name: [] for name in FEATURE_NAMES}
    used_dates: list[str] = []
    for snapshot in snapshots:
        payload = load_feature_snapshot_artifact(str(snapshot["path"]))
        overrides = transform_state.apply(
            str(snapshot["decision_date"]),
            payload,
        )
        snapshot_ics = _snapshot_rank_ic(
            str(snapshot["path"]),
            label_horizon,
            standardization,
            payload=payload,
            raw_input_overrides=overrides,
            feature_transform_key=transform_key,
        )
        if snapshot_ics is None:
            continue
        used_dates.append(str(snapshot["decision_date"]))
        for name, value in snapshot_ics.items():
            feature_ics[name].append(value)

    ic_statistics = {
        name: (
            summarize_ic_series(
                values,
                recency_halflife=recency_halflife,
                recency_mode=recency_mode,
                conflict_scale=conflict_scale,
            )
            if recency_halflife is not None
            else summarize_ic_series(values)
        )
        for name, values in feature_ics.items()
    }
    return _trained_weights_from_statistics(
        base_model_config=base_model_config,
        ic_statistics=ic_statistics,
        start_date=start_date,
        end_date=end_date,
        used_dates=used_dates,
        label_horizon=label_horizon,
        max_feature_weight=max_feature_weight,
    )


def _snapshot_rank_ic(
    snapshot_path: str,
    label_horizon: int,
    standardization: Mapping[str, Any] | None = None,
    *,
    payload: Mapping[str, Any] | None = None,
    raw_input_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    feature_transform_key: str = "",
) -> dict[str, float] | None:
    settings = normalize_standardization_config(standardization)
    cache_key = (
        snapshot_path,
        int(label_horizon),
        _standardization_cache_key(settings),
        feature_transform_key,
    )
    if cache_key in _SNAPSHOT_RANK_IC_CACHE:
        return _SNAPSHOT_RANK_IC_CACHE[cache_key]
    payload = (
        load_feature_snapshot_artifact(snapshot_path)
        if payload is None
        else payload
    )
    rows = payload.get("rows")
    if not isinstance(rows, list):
        _SNAPSHOT_RANK_IC_CACHE[cache_key] = None
        return None
    usable: list[Mapping[str, Any]] = []
    returns: list[float] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("replay_ready") is not True:
            continue
        labels = row.get("labels")
        if not isinstance(labels, Mapping):
            continue
        value = label_value(labels, label_horizon)
        if value is None:
            continue
        usable.append(row)
        returns.append(value)
    if len(usable) < 3:
        _SNAPSHOT_RANK_IC_CACHE[cache_key] = None
        return None
    result: dict[str, float] = {}
    for name in FEATURE_NAMES:
        available: list[tuple[int, float]] = []
        industries: list[str] = []
        market_caps: list[float] = []
        for index, row in enumerate(usable):
            code = str(row.get("code") or "")
            raw_inputs = row.get("raw_inputs")
            if isinstance(raw_inputs, Mapping):
                raw_inputs = apply_raw_input_overrides(
                    raw_inputs,
                    (raw_input_overrides or {}).get(code),
                    FEATURE_NAMES,
                )
            value = raw_inputs.get(name) if isinstance(raw_inputs, Mapping) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                value = None
            if value is not None:
                available.append((index, float(value)))
                standardization_inputs = row.get("standardization_inputs")
                inputs = (
                    standardization_inputs
                    if isinstance(standardization_inputs, Mapping)
                    else {}
                )
                industries.append(str(inputs.get("industry") or "__unavailable__"))
                market_value = inputs.get("market_cap")
                if isinstance(market_value, (int, float)) and not isinstance(
                    market_value, bool
                ):
                    market_caps.append(float(market_value))
        standardized = standardize_values(
            [value for _, value in available],
            config=settings,
            industries=industries if industries else None,
            market_caps=market_caps if market_caps else None,
        )
        left = [0.0] * len(usable)
        for position, (index, _) in enumerate(available):
            left[index] = standardized[position]
        result[name] = _rank_ic(left, returns)
    cached = result if result else None
    _SNAPSHOT_RANK_IC_CACHE[cache_key] = cached
    return cached


def _compute_rank_ic_summary(
    snapshot_dir: str | Path,
    *,
    start_date: str,
    end_date: str,
    label_horizon: int,
    standardization: Mapping[str, Any] | None = None,
    recency_halflife: float | None = None,
    recency_mode: str = "direction_and_strength",
    conflict_scale: float = 0.25,
    feature_transform: Mapping[str, Any] | None = None,
    snapshots: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    selected_snapshots = (
        [
            item
            for item in snapshots
            if start_date <= str(item["decision_date"]) <= end_date
        ]
        if snapshots is not None
        else discover_snapshots(
            snapshot_dir,
            start_date=start_date,
            end_date=end_date,
        )
    )
    normalized_transform = normalize_feature_transform(feature_transform)
    transform_state = FeatureTransformState(normalized_transform)
    transform_key = _feature_transform_cache_key(normalized_transform)
    feature_ics: dict[str, list[float]] = {name: [] for name in FEATURE_NAMES}
    used_dates: list[str] = []
    ordered_snapshots = sorted(
        selected_snapshots,
        key=lambda item: str(item.get("decision_date") or ""),
    )
    for snapshot in ordered_snapshots:
        payload = load_feature_snapshot_artifact(str(snapshot["path"]))
        overrides = transform_state.apply(
            str(snapshot["decision_date"]),
            payload,
        )
        snapshot_ics = _snapshot_rank_ic(
            str(snapshot["path"]),
            label_horizon,
            standardization,
            payload=payload,
            raw_input_overrides=overrides,
            feature_transform_key=transform_key,
        )
        if snapshot_ics is None:
            continue
        used_dates.append(str(snapshot["decision_date"]))
        for name, value in snapshot_ics.items():
            feature_ics[name].append(value)
    return (
        {
            name: (
                summarize_ic_series(
                    values,
                    recency_halflife=recency_halflife,
                    recency_mode=recency_mode,
                    conflict_scale=conflict_scale,
                )
                if recency_halflife is not None
                else summarize_ic_series(values)
            )
            for name, values in feature_ics.items()
        },
        used_dates,
    )


def _fit_candidate_weights_cached(
    *,
    base_model_config: Mapping[str, Any],
    snapshot_dir: str | Path,
    start_date: str,
    end_date: str,
    label_horizon: int = 5,
    max_feature_weight: float = 0.75,
    snapshots: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    standardization = normalize_standardization_config(
        base_model_config.get("standardization")
    )
    recency_halflife, recency_mode, conflict_scale = _training_recency_settings(
        base_model_config
    )
    feature_transform = normalize_feature_transform(
        base_model_config.get("feature_transform")
    )
    transform_key = _feature_transform_cache_key(feature_transform)
    cache_key = (
        str(Path(snapshot_dir).resolve(strict=False)),
        str(start_date),
        str(end_date),
        int(label_horizon),
        float(max_feature_weight),
        _standardization_cache_key(standardization),
        float(recency_halflife or 0.0),
        recency_mode,
        float(conflict_scale),
        transform_key,
    )
    cached = _RANK_IC_CACHE.get(cache_key)
    if cached is None:
        summary_kwargs = {
            "snapshot_dir": snapshot_dir,
            "start_date": start_date,
            "end_date": end_date,
            "label_horizon": label_horizon,
            "standardization": standardization,
            "snapshots": snapshots,
        }
        if recency_halflife is not None:
            summary_kwargs["recency_halflife"] = recency_halflife
            summary_kwargs["recency_mode"] = recency_mode
            summary_kwargs["conflict_scale"] = conflict_scale
        if feature_transform:
            summary_kwargs["feature_transform"] = feature_transform
        cached = _compute_rank_ic_summary(**summary_kwargs)
        _RANK_IC_CACHE[cache_key] = cached
    ic_statistics, used_dates = cached
    return _trained_weights_from_statistics(
        base_model_config=base_model_config,
        ic_statistics=ic_statistics,
        start_date=start_date,
        end_date=end_date,
        used_dates=used_dates,
        label_horizon=label_horizon,
        max_feature_weight=max_feature_weight,
    )


def _evaluate_candidate(
    *,
    database: str | Path,
    candidate: Mapping[str, Any],
    windows: Sequence[Mapping[str, Any]],
    snapshot_dir: str | Path,
    snapshots: Sequence[Mapping[str, Any]],
    bars: Any,
    objective: Mapping[str, Any],
    output_dir: Path,
    code_version: str,
    portfolio_runner: Callable[..., Mapping[str, Any]],
    training_mode: str,
    label_horizon: int,
    max_feature_weight: float,
    trade_count_policy: Mapping[str, Any],
) -> dict[str, Any]:
    fold_results: list[dict[str, Any]] = []
    fold_objective = objective_with_fold_trade_minimum(
        objective,
        int(trade_count_policy["fold_min_trade_count"]),
    )
    for window in windows:
        fold = int(window["fold"])
        output_path = output_dir / f"fold-{fold:02d}.json"
        try:
            fold_model_config = candidate["model_config"]
            training: dict[str, Any] | None = None
            if training_mode == "rank_ic":
                fold_model_config, training = _fit_candidate_weights_cached(
                    base_model_config=candidate["model_config"],
                    snapshot_dir=snapshot_dir,
                    start_date=window["train_start"],
                    end_date=window["train_end"],
                    label_horizon=label_horizon,
                    max_feature_weight=max_feature_weight,
                    snapshots=snapshots,
                )
            portfolio = portfolio_runner(
                database,
                fold_model_config,
                snapshot_dir,
                start_date=window["validation_start"],
                end_date=window["validation_end"],
                bars=bars,
                config=candidate["backtest_config"],
                output_path=output_path,
                code_version=code_version,
            )
            scored = score_portfolio_objective(portfolio["metrics"], fold_objective)
            fold_results.append(
                {
                    "fold": fold,
                    "train_start": window["train_start"],
                    "train_end": window["train_end"],
                    "validation_start": window["validation_start"],
                    "validation_end": window["validation_end"],
                    "status": scored["status"],
                    "objective_score": scored["score"],
                    "components": scored["components"],
                    "hard_gate_failures": scored["hard_gate_failures"],
                    "sample_requirements": scored["sample_requirements"],
                    "unavailable_reason": scored["unavailable_reason"],
                    "metrics": dict(portfolio["metrics"]),
                    "trained_model_config": fold_model_config,
                    "training": training,
                    "portfolio_run_id": portfolio.get("run_id"),
                    "portfolio_output_path": portfolio.get("output_path"),
                }
            )
        except Exception as exc:
            fold_results.append(
                {
                    "fold": fold,
                    "train_start": window["train_start"],
                    "train_end": window["train_end"],
                    "validation_start": window["validation_start"],
                    "validation_end": window["validation_end"],
                    "status": "failed",
                    "objective_score": None,
                    "components": {},
                    "hard_gate_failures": ["runtime_error"],
                    "sample_requirements": None,
                    "unavailable_reason": "runtime_error",
                    "metrics": {},
                    "error": str(exc),
                }
            )
    return {
        **candidate,
        "validation": _aggregate_fold_results(
            fold_results,
            trade_count_policy=trade_count_policy,
        ),
        "weight_training": _summarize_weight_training(fold_results),
        "folds": fold_results,
    }


def run_weight_learning_audit(
    *,
    snapshot_dir: str | Path,
    base_model_config: Mapping[str, Any],
    start_date: str | None = None,
    end_date: str | None = None,
    walk_forward: Mapping[str, Any] | None = None,
    label_horizon: int = 5,
    max_feature_weight: float = 0.75,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_WEIGHT_AUDIT_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_WEIGHT_AUDIT_REPORT_DIR,
) -> dict[str, Any]:
    """Fit real Phase 4 weights without running portfolio validation or promotion."""
    base = validate_candidate_config(base_model_config)
    snapshot_dir = Path(snapshot_dir).resolve(strict=False)
    snapshots = discover_snapshots(
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    walk_forward_source = dict(walk_forward or {})
    if walk_forward is None:
        walk_forward_source = {"purge_sessions": 5, "embargo_sessions": 5}
    schedule = build_walk_forward_windows(
        [item["decision_date"] for item in snapshots],
        {
            **walk_forward_source,
            "training_mode": "rank_ic",
            "label_horizon": int(label_horizon),
        },
    )
    fold_training: list[dict[str, Any]] = []
    for window in schedule["folds"]:
        model, training = _fit_candidate_weights_cached(
            base_model_config=base,
            snapshot_dir=snapshot_dir,
            start_date=window["train_start"],
            end_date=window["train_end"],
            label_horizon=int(label_horizon),
            max_feature_weight=float(max_feature_weight),
            snapshots=snapshots,
        )
        fold_training.append(
            {
                "fold": int(window["fold"]),
                "train_start": window["train_start"],
                "train_end": window["train_end"],
                "model_config": model,
                "training": training,
            }
        )
    final_model, final_training = _fit_candidate_weights_cached(
        base_model_config=base,
        snapshot_dir=snapshot_dir,
        start_date=schedule["holdout"]["train_start"],
        end_date=schedule["holdout"]["train_end"],
        label_horizon=int(label_horizon),
        max_feature_weight=float(max_feature_weight),
        snapshots=snapshots,
    )
    directions: dict[str, list[int]] = {name: [] for name in FEATURE_NAMES}
    maxima: list[float] = []
    for item in fold_training:
        training = item["training"]
        for name, value in training["feature_directions"].items():
            directions[name].append(int(value))
        maxima.append(float(training["max_feature_weight"]))
    cross_window_consistency = {}
    for name, values in directions.items():
        positive = sum(value > 0 for value in values)
        cross_window_consistency[name] = (
            max(positive, len(values) - positive) / len(values) if values else 0.0
        )
    identity = {
        "snapshot_manifest_hash": sha256_file(snapshot_dir / "manifest.json"),
        "base_model_config": base,
        "walk_forward": schedule["settings"],
        "label_horizon": int(label_horizon),
        "max_feature_weight": float(max_feature_weight),
        "fold_training": fold_training,
        "final_training": final_training,
    }
    audit_id = sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))
    payload = {
        "artifact_type": "kinace_v3_phase4_weight_learning_audit",
        "schema_version": SCHEMA_VERSION,
        "audit_id": audit_id,
        "status": "completed",
        "snapshot_dir": str(snapshot_dir),
        "snapshot_count": len(snapshots),
        "date_range": {
            "start": snapshots[0]["decision_date"] if snapshots else None,
            "end": snapshots[-1]["decision_date"] if snapshots else None,
        },
        "label_horizon": int(label_horizon),
        "max_feature_weight_budget": float(max_feature_weight),
        "cross_window_direction_consistency": cross_window_consistency,
        "minimum_cross_window_direction_consistency": min(
            cross_window_consistency.values()
        )
        if cross_window_consistency
        else 0.0,
        "fold_max_feature_weight": round(max(maxima), 12) if maxima else None,
        "fold_training": fold_training,
        "final_training": final_training,
        "final_model_config": final_model,
        "holdout_used_for_weight_selection": False,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False) / f"weight-audit-{audit_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False) / f"weight-audit-{audit_id}.md"
    )
    lines = [
        "# Kinace Phase 4 Weight Learning Audit",
        "",
        f"- Audit ID: `{audit_id}`",
        f"- Snapshots: `{len(snapshots)}`",
        f"- Label horizon: `{label_horizon}`",
        f"- Max feature weight budget: `{max_feature_weight}`",
        f"- Minimum cross-window direction consistency: `{payload['minimum_cross_window_direction_consistency']}`",
        "",
        "## Final Training Weights",
        "",
        "| Feature | ICIR | Sign consistency | Direction | Weight |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in FEATURE_NAMES:
        statistics_payload = final_training["ic_statistics"][name]
        lines.append(
            f"| `{name}` | {statistics_payload['icir']} | "
            f"{statistics_payload['sign_consistency']} | "
            f"{final_training['feature_directions'][name]} | "
            f"{final_training['trained_weights'][name]} |"
        )
    lines.extend(
        [
            "",
            f"- Final max feature weight: `{final_training['max_feature_weight']}`",
            f"- Max-weight satisfied: `{final_training['max_feature_weight_satisfied']}`",
            "",
        ]
    )
    _write_text("\n".join(lines), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    return payload


def run_research_loop(
    database: str | Path | None,
    payload: Mapping[str, Any],
    *,
    snapshot_dir: str | Path,
    start_date: str | None = None,
    end_date: str | None = None,
    bars: Any | None = None,
    output_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_RESEARCH_LOOP_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_RESEARCH_LOOP_REPORT_DIR,
    code_version: str = "kinace-v3",
    max_rounds: int | None = None,
    stability_config: Mapping[str, Any] | None = None,
    holdout_registry_path: str | Path | None = None,
    holdout_access_registry_path: str | Path | None = None,
    holdout_track: str | None = None,
    portfolio_runner: Callable[..., Mapping[str, Any]] = run_portfolio_backtest,
) -> dict[str, Any]:
    """Run multi-round candidate generation, walk-forward validation, and holdout."""
    source = dict(_mapping(payload, "research loop payload"))
    normalized = normalize_optimization_payload(source)
    objective = normalize_objective_config(normalized["objective"])
    seeds = [dict(item) for item in normalized["candidates"]]
    for seed in seeds:
        seed["signature"] = _candidate_signature(
            seed["model_config"], seed["backtest_config"]
        )
    walk_forward = normalize_walk_forward_config(
        dict(source.get("walk_forward") or {}) if "walk_forward" in source else None
    )
    if max_rounds is not None:
        walk_forward["max_rounds"] = _positive_int(
            max_rounds, "max_rounds", maximum=MAX_RESEARCH_ROUNDS
        )
    model_space = dict(_mapping(source.get("model_search_space") or {}, "model_search_space"))
    backtest_space = dict(
        _mapping(source.get("backtest_search_space") or {}, "backtest_search_space")
    )
    stability_source = (
        stability_config if stability_config is not None else source.get("stability")
    )
    stability_settings = normalize_stability_config(
        dict(stability_source)
        if isinstance(stability_source, Mapping)
        else None
    )
    holdout_policy = normalize_holdout_extension_policy(
        dict(source["holdout_policy"])
        if isinstance(source.get("holdout_policy"), Mapping)
        else None
    )

    decision_dates = discover_snapshot_dates(
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    schedule = build_walk_forward_windows(
        decision_dates,
        walk_forward,
    )
    development_snapshots = discover_snapshots(
        snapshot_dir,
        start_date=start_date,
        end_date=schedule["holdout"]["train_end"],
    )
    target = database_path() if database is None else validate_database_path(database)
    initialize_research_loop_schema(target)
    if holdout_track is None:
        from .synthetic_sandbox import active_track

        resolved_holdout_track = active_track(
            target,
            snapshot_dir,
            output_dir,
            holdout_access_registry_path,
        )
    else:
        resolved_holdout_track = str(holdout_track).strip().casefold()
    if resolved_holdout_track not in {"formal", "sandbox"}:
        raise HoldoutAccessError("holdout track must be formal or sandbox")
    if resolved_holdout_track == "sandbox" and holdout_access_registry_path is None:
        from .synthetic_sandbox import MANIFEST_FILENAME

        for candidate_path in (target, snapshot_dir, output_dir):
            resolved_candidate = Path(candidate_path).resolve(strict=False)
            for parent in (resolved_candidate, *resolved_candidate.parents):
                if (parent / MANIFEST_FILENAME).is_file():
                    holdout_access_registry_path = parent / SANDBOX_REGISTRY_FILENAME
                    break
            if holdout_access_registry_path is not None:
                break
    resolved_holdout_access_registry_path = resolve_holdout_access_registry_path(
        holdout_access_registry_path,
        track=resolved_holdout_track,
    )
    loop_id = _loop_id_for(
        code_version=code_version,
        seeds=seeds,
        objective=objective,
        walk_forward=walk_forward,
        holdout_policy=holdout_policy,
        model_space=model_space,
        backtest_space=backtest_space,
        snapshot_dir=snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    superseded = archive_active_challengers(
        target,
        superseded_by_loop_id=loop_id,
        reason="V3.3 Phase 0 protocol freeze; prior challenger evidence is invalidated",
    )
    evaluation_protocol = build_effective_research_protocol(
        loop_id=loop_id,
        code_version=code_version,
        objective=objective,
        walk_forward=walk_forward,
        stability_config=stability_settings,
        seeds=seeds,
        model_search_space=model_space,
        backtest_search_space=backtest_space,
        walk_forward_defaults=DEFAULT_WALK_FORWARD,
        holdout_policy=holdout_policy,
        evaluation_policy=(
            dict(source["evaluation_policy"])
            if isinstance(source.get("evaluation_policy"), Mapping)
            else None
        ),
    )
    loop_dir = Path(output_dir).resolve(strict=False) / loop_id
    loop_dir.mkdir(parents=True, exist_ok=True)
    protocol_artifacts = write_effective_protocol(evaluation_protocol, loop_dir)
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(report_dir).resolve(strict=False) / f"research-loop-{loop_id}.json"
    )
    shared_bars = normalize_market_bars(
        bars if bars is not None else load_market_bars(target)
    )
    evaluated: dict[str, dict[str, Any]] = {}
    rounds: list[dict[str, Any]] = []
    current_candidates = seeds[: walk_forward["candidates_per_round"]]

    for round_number in range(walk_forward["max_rounds"]):
        evaluated_round: list[dict[str, Any]] = []
        for candidate_index, candidate in enumerate(current_candidates):
            signature = candidate["signature"]
            if signature in evaluated:
                evaluated_round.append(evaluated[signature])
                continue
            result = _evaluate_candidate(
                database=target,
                candidate=candidate,
                windows=schedule["folds"],
                snapshot_dir=snapshot_dir,
                snapshots=development_snapshots,
                bars=shared_bars,
                objective=objective,
                output_dir=loop_dir / f"round-{round_number:02d}" / f"candidate-{candidate_index:03d}",
                code_version=code_version,
                portfolio_runner=portfolio_runner,
                training_mode=walk_forward["training_mode"],
                label_horizon=walk_forward["label_horizon"],
                max_feature_weight=stability_settings["max_feature_weight"],
                trade_count_policy=stability_settings,
            )
            evaluated[signature] = result
            evaluated_round.append(result)
        evaluated_round.sort(key=_candidate_rank_key, reverse=True)
        round_eligible = [
            item
            for item in evaluated_round
            if item["validation"]["status"] != "unavailable"
        ]
        rounds.append(
            {
                "round": round_number,
                "candidate_count": len(evaluated_round),
                "best_candidate_signature": round_eligible[0]["signature"]
                if round_eligible
                else None,
                "candidates": [
                    {
                        "signature": item["signature"],
                        "model_config": item["model_config"],
                        "backtest_config": item["backtest_config"],
                        "validation": item["validation"],
                        "weight_training": item.get("weight_training"),
                        "folds": item["folds"],
                    }
                    for item in evaluated_round
                ],
            }
        )
        if round_number + 1 >= walk_forward["max_rounds"] or not round_eligible:
            break
        if not model_space and not backtest_space:
            break
        neighbors = _parameter_neighbors(
            model_config=round_eligible[0]["model_config"],
            backtest_config=round_eligible[0]["backtest_config"],
            model_space=model_space,
            backtest_space=backtest_space,
        )
        current_candidates = [
            item
            for item in neighbors
            if item["signature"] not in evaluated
        ][: walk_forward["candidates_per_round"]]
        if not current_candidates:
            break

    all_candidates = sorted(evaluated.values(), key=_candidate_rank_key, reverse=True)
    eligible_candidates = [
        candidate
        for candidate in all_candidates
        if candidate["validation"]["status"] == "validated"
    ]
    best = eligible_candidates[0] if eligible_candidates else None
    holdout_eligibility = {
        "eligible": best is not None,
        "state": "eligible" if best is not None else "not_eligible",
        "non_holdout_gates_passed": best is not None,
        "candidate": best["signature"] if best is not None else None,
        "candidate_status": (
            best["validation"]["status"] if best is not None else "not_selected"
        ),
        "evaluated_candidate_count": len(all_candidates),
        "eligible_candidate_count": len(eligible_candidates),
    }
    holdout: dict[str, Any] | None = None
    holdout_access: dict[str, Any] | None = None
    holdout_status = "not_run"
    holdout_extension_freeze: dict[str, Any] | None = None
    final_training: dict[str, Any] | None = None
    if best is not None:
        holdout_model_config = best["model_config"]
        if walk_forward["training_mode"] == "rank_ic":
            holdout_model_config, final_training = _fit_candidate_weights_cached(
                base_model_config=best["model_config"],
                snapshot_dir=snapshot_dir,
                start_date=schedule["holdout"]["train_start"],
                end_date=schedule["holdout"]["train_end"],
                label_horizon=walk_forward["label_horizon"],
                max_feature_weight=stability_settings["max_feature_weight"],
                snapshots=development_snapshots,
            )
        holdout_access = open_holdout_access(
            best["signature"],
            evaluation_protocol["evaluation_protocol_hash"],
            schedule["holdout"],
            eligible=True,
            registry_path=resolved_holdout_access_registry_path,
            track=resolved_holdout_track,
        )
        holdout_output = loop_dir / "holdout.json"
        try:
            holdout_portfolio = portfolio_runner(
                target,
                holdout_model_config,
                snapshot_dir,
                start_date=schedule["holdout"]["start"],
                end_date=schedule["holdout"]["end"],
                bars=shared_bars,
                config=best["backtest_config"],
                output_path=holdout_output,
                code_version=code_version,
            )
            holdout_score = score_portfolio_objective(
                holdout_portfolio["metrics"], objective
            )
            holdout_base_status = str(holdout_score["status"])
            extension_freeze_artifacts: dict[str, Any] = {}
            if (
                holdout_base_status in {"rejected", "unavailable"}
                and holdout_score["hard_gate_failures"] == ["min_trade_count"]
            ):
                non_trade_score = score_portfolio_objective(
                    holdout_portfolio["metrics"],
                    objective_without_trade_count(objective),
                )
                if non_trade_score["status"] == "passed":
                    candidate_protocol_id = protocol_candidate_id(
                        str(evaluation_protocol["evaluation_protocol_hash"]),
                        best["signature"],
                    )
                    holdout_extension_freeze = build_holdout_extension_freeze(
                        loop_id=loop_id,
                        candidate_signature=best["signature"],
                        protocol_candidate_id=candidate_protocol_id,
                        evaluation_protocol_hash=str(
                            evaluation_protocol["evaluation_protocol_hash"]
                        ),
                        model_config=holdout_model_config,
                        backtest_config=best["backtest_config"],
                        objective=objective,
                        initial_holdout=schedule["holdout"],
                        initial_metrics=holdout_portfolio["metrics"],
                        initial_objective=non_trade_score,
                        policy=holdout_policy,
                        holdout_access_id=holdout_access["holdout_access_id"],
                        track=resolved_holdout_track,
                        holdout_access_registry_path=resolved_holdout_access_registry_path,
                    )
                    extension_freeze_artifacts = write_holdout_extension_freeze(
                        holdout_extension_freeze,
                        loop_dir / "holdout-extension-freeze.json",
                    )
                    extension_freeze_artifacts["registry_record"] = (
                        register_holdout_extension_freeze(
                            holdout_extension_freeze,
                            database=target,
                            freeze_path=extension_freeze_artifacts["freeze_path"],
                            registry_path=holdout_registry_path,
                        )
                    )
            holdout = {
                "status": (
                    "extension_pending"
                    if holdout_extension_freeze is not None
                    else holdout_base_status
                ),
                "base_status": holdout_base_status,
                "objective_score": holdout_score["score"],
                "components": holdout_score["components"],
                "hard_gate_failures": holdout_score["hard_gate_failures"],
                "sample_requirements": holdout_score["sample_requirements"],
                "unavailable_reason": holdout_score["unavailable_reason"],
                "metrics": dict(holdout_portfolio["metrics"]),
                "portfolio_run_id": holdout_portfolio.get("run_id"),
                "portfolio_output_path": holdout_portfolio.get("output_path"),
                "date_range": schedule["holdout"],
                "model_config": holdout_model_config,
                "training": final_training,
                "holdout_access_id": holdout_access["holdout_access_id"],
                "holdout_access_read_count": holdout_access["read_count"],
                "holdout_track": resolved_holdout_track,
                **extension_freeze_artifacts,
            }
            holdout_status = (
                "challenger_ready"
                if holdout_base_status == "passed"
                else (
                    "holdout_extension_pending"
                    if holdout_extension_freeze is not None
                    else "holdout_rejected"
                )
            )
        except Exception as exc:
            holdout = {
                "status": "failed",
                "objective_score": None,
                "hard_gate_failures": ["runtime_error"],
                "sample_requirements": None,
                "unavailable_reason": "runtime_error",
                "metrics": {},
                "error": str(exc),
                "date_range": schedule["holdout"],
            }
            holdout_status = "holdout_failed"

        best = {
            **best,
            "base_model_config": best["model_config"],
            "model_config": holdout_model_config,
            "final_training": final_training,
            "final_weight_training": (
                {
                    "feature_directions": final_training.get("feature_directions"),
                    "max_feature_weight": final_training.get("max_feature_weight"),
                    "max_feature_weight_satisfied": final_training.get(
                        "max_feature_weight_satisfied"
                    ),
                }
                if final_training
                else None
            ),
        }

    evaluation_protocol_hash = str(
        evaluation_protocol["evaluation_protocol_hash"]
    )
    if best is not None:
        best = {
            **best,
            "protocol_candidate_id": protocol_candidate_id(
                evaluation_protocol_hash, best["signature"]
            ),
        }
    candidate_manifest = build_candidate_config_manifest(
        loop_id=loop_id,
        evaluation_protocol_hash=evaluation_protocol_hash,
        seed_candidates=seeds,
        model_search_space=model_space,
        backtest_search_space=backtest_space,
        rounds=rounds,
        observed_candidate_count=len(evaluated),
    )
    manifest_artifacts = write_candidate_manifest(candidate_manifest, loop_dir)

    status = (
        "completed"
        if holdout_status == "challenger_ready"
        else "no_eligible_challenger"
    )
    report = {
        "artifact_type": "kinace_v3_automated_research_loop",
        "schema_version": SCHEMA_VERSION,
        "loop_id": loop_id,
        "status": status,
        "database": str(target),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "date_range": {"start": start_date, "end": end_date},
        "walk_forward": schedule,
        "objective": objective,
        "round_count": len(rounds),
        "candidate_count": len(evaluated),
        "rounds": rounds,
        "holdout_eligibility": holdout_eligibility,
        "best_candidate": best,
        "holdout": holdout,
        "holdout_access": holdout_access,
        "holdout_access_id": (
            holdout_access["holdout_access_id"] if holdout_access is not None else None
        ),
        "holdout_access_read_count": (
            holdout_access["read_count"] if holdout_access is not None else 0
        ),
        "holdout_access_registry_path": str(resolved_holdout_access_registry_path),
        "holdout_track": resolved_holdout_track,
        "holdout_status": holdout_status,
        "evaluation_protocol_hash": evaluation_protocol_hash,
        "evaluation_protocol": evaluation_protocol,
        "protocol_binding": {
            **protocol_artifacts,
            **manifest_artifacts,
        },
        "superseded_candidates": superseded["superseded_candidates"],
        "superseded_candidate_count": len(superseded["superseded_candidates"]),
        "candidate_audit_required": (
            best is not None and holdout_status == "challenger_ready"
        ),
        "candidate_registration_status": (
            "awaiting_candidate_audit"
            if best is not None and holdout_status == "challenger_ready"
            else (
                "awaiting_holdout_extension"
                if best is not None
                and holdout_status == "holdout_extension_pending"
                else "not_registered"
            )
        ),
        "production_ready": False,
        "production_changed": False,
        "completed_at_utc": _utc_now(),
    }
    challenger_path: str | None = None
    if best is not None:
        challenger = {
            "artifact_type": "kinace_v3_research_challenger",
            "loop_id": loop_id,
            "candidate_signature": best["signature"],
            "protocol_candidate_id": best["protocol_candidate_id"],
            "evaluation_protocol_hash": evaluation_protocol_hash,
            "model_config": best["model_config"],
            "backtest_config": best["backtest_config"],
            "validation": best["validation"],
            "holdout_status": holdout_status,
            "holdout": holdout,
            "holdout_access_id": (
                holdout_access["holdout_access_id"]
                if holdout_access is not None
                else None
            ),
            "candidate_audit_required": holdout_status == "challenger_ready",
            "candidate_registration_status": (
                "awaiting_candidate_audit"
                if holdout_status == "challenger_ready"
                else (
                    "awaiting_holdout_extension"
                    if holdout_status == "holdout_extension_pending"
                    else "not_registered"
                )
            ),
            "production_ready": False,
            "production_changed": False,
        }
        challenger_file = _write_json(challenger, loop_dir / "best-challenger.json")
        challenger_path = str(challenger_file)
    report["best_challenger_path"] = challenger_path
    written = _write_json(report, artifact_path)
    report["artifact_path"] = str(written)
    report["artifact_sha256"] = sha256_file(written)

    initialize_research_loop_schema(target)
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO research_loop_runs (
                loop_id, code_version, status, fold_count, round_count,
                candidate_count, best_candidate_signature, holdout_run_id,
                holdout_status, artifact_path, artifact_sha256,
                evaluation_protocol_hash, protocol_artifact_path,
                protocol_artifact_sha256, candidate_manifest_path,
                candidate_manifest_sha256, superseded_candidate_count,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(loop_id) DO UPDATE SET
                code_version=excluded.code_version,
                status=excluded.status,
                fold_count=excluded.fold_count,
                round_count=excluded.round_count,
                candidate_count=excluded.candidate_count,
                best_candidate_signature=excluded.best_candidate_signature,
                holdout_run_id=excluded.holdout_run_id,
                holdout_status=excluded.holdout_status,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                evaluation_protocol_hash=excluded.evaluation_protocol_hash,
                protocol_artifact_path=excluded.protocol_artifact_path,
                protocol_artifact_sha256=excluded.protocol_artifact_sha256,
                candidate_manifest_path=excluded.candidate_manifest_path,
                candidate_manifest_sha256=excluded.candidate_manifest_sha256,
                superseded_candidate_count=excluded.superseded_candidate_count
            """,
            (
                loop_id,
                code_version,
                status,
                len(schedule["folds"]),
                len(rounds),
                len(evaluated),
                best["signature"] if best else None,
                (holdout or {}).get("portfolio_run_id"),
                holdout_status,
                str(written),
                report["artifact_sha256"],
                evaluation_protocol_hash,
                protocol_artifacts["effective_protocol_path"],
                protocol_artifacts["effective_protocol_sha256"],
                manifest_artifacts["candidate_manifest_path"],
                manifest_artifacts["candidate_manifest_sha256"],
                len(superseded["superseded_candidates"]),
                _utc_now(),
            ),
        )
        connection.execute(
            "DELETE FROM research_candidate_rounds WHERE loop_id=?",
            (loop_id,),
        )
        for round_payload in rounds:
            for candidate in round_payload["candidates"]:
                connection.execute(
                    """
                    INSERT INTO research_candidate_rounds (
                        loop_id, round_number, candidate_signature, status,
                        mean_score, min_score, pass_rate, fold_results_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        loop_id,
                        int(round_payload["round"]),
                        candidate["signature"],
                        candidate["validation"]["status"],
                        candidate["validation"]["mean_score"],
                        candidate["validation"]["min_score"],
                        candidate["validation"]["pass_rate"],
                        canonical_json_bytes(
                            candidate["folds"], trailing_newline=False
                        ).decode("utf-8"),
                    ),
                )
        for candidate in all_candidates:
            persisted_model_config = (
                best["model_config"]
                if best is not None
                and candidate["signature"] == best["signature"]
                and "model_config" in best
                else candidate["model_config"]
            )
            persisted_backtest_config = (
                best["backtest_config"]
                if best is not None
                and candidate["signature"] == best["signature"]
                and "backtest_config" in best
                else candidate["backtest_config"]
            )
            is_best = best is not None and candidate["signature"] == best["signature"]
            if is_best and holdout_status == "challenger_ready":
                stage = "awaiting_candidate_audit"
            elif is_best:
                stage = holdout_status
            else:
                stage = candidate["validation"]["status"]
            connection.execute(
                """
                INSERT INTO research_candidates (
                    candidate_signature, protocol_candidate_id,
                    evaluation_protocol_hash, code_version, model_config_json,
                    backtest_config_json, stage, latest_loop_id,
                    best_objective_score, holdout_status, holdout_metrics_json,
                    superseded_by_loop_id, superseded_reason, superseded_at_utc,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(candidate_signature) DO UPDATE SET
                    protocol_candidate_id=excluded.protocol_candidate_id,
                    evaluation_protocol_hash=excluded.evaluation_protocol_hash,
                    code_version=excluded.code_version,
                    model_config_json=excluded.model_config_json,
                    backtest_config_json=excluded.backtest_config_json,
                    stage=excluded.stage,
                    latest_loop_id=excluded.latest_loop_id,
                    best_objective_score=excluded.best_objective_score,
                    holdout_status=excluded.holdout_status,
                    holdout_metrics_json=excluded.holdout_metrics_json,
                    superseded_by_loop_id=NULL,
                    superseded_reason=NULL,
                    superseded_at_utc=NULL,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    candidate["signature"],
                    protocol_candidate_id(evaluation_protocol_hash, candidate["signature"]),
                    evaluation_protocol_hash,
                    code_version,
                    canonical_json_bytes(
                        persisted_model_config, trailing_newline=False
                    ).decode("utf-8"),
                    canonical_json_bytes(
                        persisted_backtest_config, trailing_newline=False
                    ).decode("utf-8"),
                    stage,
                    loop_id,
                    candidate["validation"]["mean_score"],
                    holdout_status
                    if is_best
                    else None,
                    canonical_json_bytes(
                        holdout.get("metrics") or {}
                        if is_best
                        else {},
                        trailing_newline=False,
                    ).decode("utf-8"),
                    _utc_now(),
                    _utc_now(),
                ),
            )
        connection.execute(
            "DELETE FROM research_evidence_runs WHERE loop_id=?",
            (loop_id,),
        )
        for round_payload in rounds:
            for candidate in round_payload["candidates"]:
                for fold in candidate.get("folds") or []:
                    run_id = fold.get("portfolio_run_id")
                    if not run_id:
                        continue
                    connection.execute(
                        """
                        INSERT INTO research_evidence_runs (
                            loop_id, candidate_signature, round_number,
                            run_kind, fold_number, run_id
                        ) VALUES (?, ?, ?, 'fold', ?, ?)
                        """,
                        (
                            loop_id,
                            candidate["signature"],
                            int(round_payload["round"]),
                            int(fold.get("fold") or 0),
                            str(run_id),
                        ),
                    )
        if best is not None and holdout and holdout.get("portfolio_run_id"):
            connection.execute(
                """
                INSERT INTO research_evidence_runs (
                    loop_id, candidate_signature, round_number,
                    run_kind, fold_number, run_id
                ) VALUES (?, ?, ?, 'holdout', -1, ?)
                """,
                (
                    loop_id,
                    best["signature"],
                    len(rounds) - 1,
                    str(holdout["portfolio_run_id"]),
                ),
            )
    return report


__all__ = [
    "DEFAULT_RESEARCH_LOOP_OUTPUT_DIR",
    "DEFAULT_RESEARCH_LOOP_REPORT_DIR",
    "DEFAULT_WEIGHT_AUDIT_OUTPUT_DIR",
    "DEFAULT_WEIGHT_AUDIT_REPORT_DIR",
    "DEFAULT_WALK_FORWARD",
    "MAX_CANDIDATES_PER_ROUND",
    "MAX_RESEARCH_ROUNDS",
    "ResearchLoopError",
    "SCHEMA_VERSION",
    "build_walk_forward_windows",
    "initialize_research_loop_schema",
    "normalize_walk_forward_config",
    "run_research_loop",
    "run_weight_learning_audit",
]
