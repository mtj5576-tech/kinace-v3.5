"""Phase 6 exit ablation, permutation, attribution, and capacity stress tests."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, validate_database_path
from .feature_snapshot import (
    FEATURE_NAMES,
    canonical_json_bytes,
    load_feature_snapshot_artifact,
    sha256_bytes,
    sha256_file,
)
from .optimization import score_portfolio_objective
from .portfolio_backtest import (
    build_portfolio_replay,
    discover_snapshots,
    load_market_bars,
)
from .research_labeling import label_value
from .research_weighting import standardize_values
from .score_replay import validate_candidate_config
from .trial_backtest import normalize_backtest_config, normalize_market_bars, simulate_trial


SCHEMA_VERSION = 1
DEFAULT_ABLATION_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_ablation"
DEFAULT_ABLATION_REPORT_DIR = PROJECT_ROOT / "reports" / "research_ablation"
DEFAULT_CAPACITY_LEVELS = (1_000_000.0, 10_000_000.0, 100_000_000.0)


class ResearchAblationError(ValueError):
    """Raised when Phase 6 stress evidence cannot be produced."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchAblationError(f"{field} must be an object")
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _load_json(path: str | Path, field: str) -> dict[str, Any]:
    target = Path(path).resolve(strict=False)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchAblationError(f"cannot load {field}: {exc}") from exc
    return dict(_mapping(payload, field))


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


def _average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + end - 1) / 2.0 + 1.0
        for position in range(index, end):
            ranks[ordered[position][0]] = rank
        index = end
    return ranks


def _rank_ic(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = statistics.fmean(left_ranks)
    right_mean = statistics.fmean(right_ranks)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_ranks, right_ranks)
    )
    left_variance = sum((value - left_mean) ** 2 for value in left_ranks)
    right_variance = sum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_variance * right_variance)
    return numerator / denominator if denominator > 0 else None


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _metric_summary(metrics: Mapping[str, Any]) -> dict[str, Any]:
    holding = metrics.get("holding_periods")
    holding = holding if isinstance(holding, Mapping) else {}
    return {
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "profit_factor": metrics.get("profit_factor"),
        "win_rate": metrics.get("win_rate"),
        "trade_count": metrics.get("trade_count"),
        "turnover": metrics.get("turnover"),
        "average_hold_sessions": holding.get("average_hold_sessions"),
        "atr_exit_rate": holding.get("atr_exit_rate"),
        "time_exit_rate": holding.get("time_exit_rate"),
        "execution_diagnostics": metrics.get("execution_diagnostics") or {},
    }


def _random_scores(scores: Sequence[Mapping[str, Any]], *, seed: int) -> list[dict[str, Any]]:
    result = []
    for row in scores:
        identity = f"{seed}|{row.get('decision_date')}|{row.get('code')}".encode("utf-8")
        digest = hashlib.sha256(identity).digest()
        value = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
        result.append(
            {
                "decision_date": row.get("decision_date"),
                "code": row.get("code"),
                "score": value,
            }
        )
    return result


def _candidate_period(loop_report: Mapping[str, Any]) -> tuple[str, str]:
    holdout = _mapping(loop_report.get("holdout") or {}, "loop_report.holdout")
    date_range = holdout.get("date_range")
    if isinstance(date_range, Mapping) and date_range.get("start") and date_range.get("end"):
        return str(date_range["start"]), str(date_range["end"])
    schedule = _mapping(loop_report.get("walk_forward") or {}, "loop_report.walk_forward")
    holdout_schedule = _mapping(schedule.get("holdout") or {}, "walk_forward.holdout")
    return str(holdout_schedule.get("start") or ""), str(holdout_schedule.get("end") or "")


def _filter_bars(
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    codes: set[str],
    end_date: str,
) -> dict[str, list[dict[str, Any]]]:
    return normalize_market_bars(
        {
            code: [
                dict(bar)
                for bar in code_bars
                if str(bar.get("trade_date") or "") <= end_date
            ]
            for code, code_bars in bars.items()
            if str(code) in codes
        }
    )


def _variant_simulation(
    *,
    scores: Sequence[Mapping[str, Any]],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    base_config: Mapping[str, Any],
    objective: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    simulation = simulate_trial(scores=scores, bars=bars, config=base_config)
    scored = score_portfolio_objective(simulation["metrics"], objective)
    return {
        "label": label,
        "config": simulation["config"],
        "metrics": dict(simulation["metrics"]),
        "summary": _metric_summary(simulation["metrics"]),
        "objective": scored,
    }


def _exit_ablations(
    *,
    scores: Sequence[Mapping[str, Any]],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    backtest_config: Mapping[str, Any],
    objective: Mapping[str, Any],
    random_seed: int,
) -> dict[str, Any]:
    base_config = normalize_backtest_config(backtest_config)
    variants = [
        ("atr_and_time", base_config, scores),
        (
            "time_only",
            normalize_backtest_config(
                {**base_config, "stop_atr_multiple": 1_000.0, "target_atr_multiple": 1_000.0}
            ),
            scores,
        ),
        (
            "atr_only",
            normalize_backtest_config({**base_config, "max_hold_sessions": 100_000}),
            scores,
        ),
        (
            "no_active_exit",
            normalize_backtest_config(
                {
                    **base_config,
                    "stop_atr_multiple": 1_000.0,
                    "target_atr_multiple": 1_000.0,
                    "max_hold_sessions": 100_000,
                }
            ),
            scores,
        ),
        (
            "random_signal_atr_time",
            base_config,
            _random_scores(scores, seed=random_seed),
        ),
    ]
    results = [
        _variant_simulation(
            scores=variant_scores,
            bars=bars,
            base_config=config,
            objective=objective,
            label=label,
        )
        for label, config, variant_scores in variants
    ]
    base_score = float(results[0]["objective"]["score"])
    for result in results:
        result["delta_j_vs_base"] = round(
            float(result["objective"]["score"]) - base_score, 8
        )
    return {"base_label": "atr_and_time", "variants": results}


def _symbolic_standardized_snapshots(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {feature: [] for feature in FEATURE_NAMES}
    for item in snapshots:
        payload = load_feature_snapshot_artifact(str(item["path"]))
        rows = [row for row in payload.get("rows") or [] if isinstance(row, Mapping)]
        for feature in FEATURE_NAMES:
            usable = []
            for row in rows:
                raw_inputs = row.get("raw_inputs")
                labels = row.get("labels")
                if not isinstance(raw_inputs, Mapping) or not isinstance(labels, Mapping):
                    continue
                value = _number(raw_inputs.get(feature))
                forward = label_value(labels, 5)
                if value is None or forward is None:
                    continue
                usable.append((value, forward))
            if len(usable) < 3:
                continue
            result[feature].append(
                {
                    "decision_date": str(payload.get("decision_date") or ""),
                    "values": standardize_values([value for value, _ in usable]),
                    "returns": [forward for _, forward in usable],
                }
            )
    return result


def _permutation_and_bootstrap_audit(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    permutations: int,
    bootstrap_iterations: int,
    date_sample_size: int,
    seed: int,
) -> dict[str, Any]:
    feature_panels = _symbolic_standardized_snapshots(snapshots)
    observed_series = {feature: [] for feature in FEATURE_NAMES}
    for feature in FEATURE_NAMES:
        for panel in feature_panels[feature]:
            value = _rank_ic(panel["values"], panel["returns"])
            if value is not None:
                observed_series[feature].append(value)
    observed_mean = {
        feature: statistics.fmean(values) if values else 0.0
        for feature, values in observed_series.items()
    }
    null_means = {feature: [] for feature in FEATURE_NAMES}
    rng = random.Random(seed)
    for _ in range(permutations):
        for feature in FEATURE_NAMES:
            means = []
            for panel in feature_panels[feature]:
                returns = list(panel["returns"])
                rng.shuffle(returns)
                value = _rank_ic(panel["values"], returns)
                if value is not None:
                    means.append(value)
            null_means[feature].append(statistics.fmean(means) if means else 0.0)
    permutation_results = {}
    for feature in FEATURE_NAMES:
        observed = observed_mean[feature]
        null = null_means[feature]
        exceed = sum(abs(value) >= abs(observed) for value in null)
        permutation_results[feature] = {
            "observed_mean_ic": observed,
            "null_mean": statistics.fmean(null) if null else 0.0,
            "null_stdev": statistics.stdev(null) if len(null) >= 2 else 0.0,
            "two_sided_p_value": (exceed + 1) / (len(null) + 1) if null else 1.0,
        }
    bootstrap_rng = random.Random(seed + 1)
    sampled_dates = min(
        max((len(feature_panels[feature]) for feature in FEATURE_NAMES), default=0),
        date_sample_size,
    )
    cross_sectional_means = {feature: [] for feature in FEATURE_NAMES}
    for _ in range(bootstrap_iterations):
        for feature in FEATURE_NAMES:
            panels = feature_panels[feature]
            if not panels or sampled_dates <= 0:
                cross_sectional_means[feature].append(0.0)
                continue
            date_indices = [
                bootstrap_rng.randrange(len(panels)) for _ in range(sampled_dates)
            ]
            daily = []
            for date_index in date_indices:
                panel = panels[date_index]
                count = len(panel["returns"])
                row_indices = [bootstrap_rng.randrange(count) for _ in range(count)]
                value = _rank_ic(
                    [panel["values"][index] for index in row_indices],
                    [panel["returns"][index] for index in row_indices],
                )
                if value is not None:
                    daily.append(value)
            cross_sectional_means[feature].append(statistics.fmean(daily) if daily else 0.0)
    cross_sectional = {}
    block_bootstrap = {}
    for feature in FEATURE_NAMES:
        values = cross_sectional_means[feature]
        cross_sectional[feature] = {
            "lower": _quantile(values, 0.025),
            "median": _quantile(values, 0.5),
            "upper": _quantile(values, 0.975),
        }
        series = observed_series[feature]
        block_size = max(1, min(len(series), int(round(len(series) ** (1.0 / 3.0)))))
        blocks = [series[index : index + block_size] for index in range(0, len(series) - block_size + 1)]
        rng_block = random.Random(seed + 2)
        means = []
        if blocks:
            for _ in range(1_000):
                sample = []
                while len(sample) < len(series):
                    sample.extend(rng_block.choice(blocks))
                means.append(statistics.fmean(sample[: len(series)]))
        block_bootstrap[feature] = {
            "block_size": block_size,
            "lower": _quantile(means, 0.025) if means else None,
            "median": _quantile(means, 0.5) if means else None,
            "upper": _quantile(means, 0.975) if means else None,
        }
    return {
        "panels": {
            feature: len(feature_panels[feature]) for feature in FEATURE_NAMES
        },
        "permutations": permutations,
        "bootstrap_iterations": bootstrap_iterations,
        "date_sample_size": sampled_dates,
        "permutation": permutation_results,
        "cross_sectional_bootstrap": cross_sectional,
        "block_bootstrap": block_bootstrap,
    }


def _factor_attribution(
    *,
    trades: Sequence[Mapping[str, Any]],
    score_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    score_map = {
        (str(row.get("decision_date") or ""), str(row.get("code") or "")): row
        for row in score_rows
    }
    totals = {
        feature: {"pnl": 0.0, "negative_pnl": 0.0, "turnover": 0.0}
        for feature in FEATURE_NAMES
    }
    attributed_trades = 0
    for trade in trades:
        score = score_map.get(
            (str(trade.get("decision_date") or ""), str(trade.get("code") or ""))
        )
        components = score.get("components") if isinstance(score, Mapping) else None
        if not isinstance(components, Mapping):
            continue
        values = {
            feature: abs(float(components.get(feature) or 0.0))
            for feature in FEATURE_NAMES
        }
        total = sum(values.values())
        if total <= 1e-18:
            continue
        pnl = float(trade.get("pnl") or 0.0)
        turnover = (
            float(trade.get("entry_price") or 0.0)
            + float(trade.get("exit_price") or 0.0)
        ) * int(trade.get("shares") or 0)
        for feature, value in values.items():
            share = value / total
            attributed_pnl = pnl * share
            totals[feature]["pnl"] += attributed_pnl
            totals[feature]["negative_pnl"] += min(attributed_pnl, 0.0)
            totals[feature]["turnover"] += turnover * share
        attributed_trades += 1
    total_pnl = sum(item["pnl"] for item in totals.values())
    total_turnover = sum(item["turnover"] for item in totals.values())
    result = {}
    for feature, values in totals.items():
        result[feature] = {
            **values,
            "share_of_net_pnl": values["pnl"] / total_pnl if total_pnl else None,
            "share_of_negative_pnl": values["negative_pnl"] / sum(
                item["negative_pnl"] for item in totals.values()
            )
            if sum(item["negative_pnl"] for item in totals.values())
            else None,
            "share_of_turnover": values["turnover"] / total_turnover
            if total_turnover
            else None,
        }
    return {"trades_attributed": attributed_trades, "factors": result}


def _capacity_audit(
    *,
    scores: Sequence[Mapping[str, Any]],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    backtest_config: Mapping[str, Any],
    capacity_levels: Sequence[float],
) -> dict[str, Any]:
    cases = []
    for capital in capacity_levels:
        config = normalize_backtest_config(
            {**backtest_config, "initial_cash": float(capital)}
        )
        simulation = simulate_trial(scores=scores, bars=bars, config=config)
        metrics = simulation["metrics"]
        diagnostics = metrics.get("execution_diagnostics") or {}
        attempts = max(1, int(diagnostics.get("entry_candidates") or 0))
        cases.append(
            {
                "initial_cash": float(capital),
                "total_return": metrics.get("total_return"),
                "final_equity": metrics.get("final_equity"),
                "trade_count": metrics.get("trade_count"),
                "turnover": metrics.get("turnover"),
                "diagnostics": diagnostics,
                "limit_up_blocked_rate": int(diagnostics.get("limit_up_blocked") or 0)
                / attempts,
                "suspended_blocked_rate": int(diagnostics.get("suspended_blocked") or 0)
                / attempts,
                "zero_volume_blocked_rate": int(diagnostics.get("zero_volume_blocked") or 0)
                / attempts,
                "volume_capped_rate": int(diagnostics.get("volume_capped") or 0)
                / attempts,
                "cash_blocked_rate": int(diagnostics.get("cash_blocked") or 0)
                / attempts,
            }
        )
    return {"levels": list(capacity_levels), "cases": cases}


def _markdown_report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Kinace Phase 6 Stress and Attribution",
        "",
        f"- Audit ID: `{payload['audit_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Candidate: `{payload['candidate_signature']}`",
        f"- Period: `{payload['period']['start']}` to `{payload['period']['end']}`",
        "",
        "## Exit Ablations",
        "",
        "| Variant | Return | Sharpe | Max DD | Trades | Delta J |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in payload["exit_ablations"]["variants"]:
        metrics = item["summary"]
        lines.append(
            f"| `{item['label']}` | {metrics['total_return']} | {metrics['sharpe']} | "
            f"{metrics['max_drawdown']} | {metrics['trade_count']} | {item['delta_j_vs_base']} |"
        )
    lines.extend(
        [
            "",
            "## Factor Attribution",
            "",
            "| Factor | PnL | Negative PnL | Turnover | Turnover Share |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for feature, item in payload["factor_attribution"]["factors"].items():
        lines.append(
            f"| `{feature}` | {item['pnl']} | {item['negative_pnl']} | "
            f"{item['turnover']} | {item['share_of_turnover']} |"
        )
    lines.extend(["", "## Capacity", "", "| Capital | Return | Trades | Limit-up blocked | Volume capped | Cash blocked |", "|---:|---:|---:|---:|---:|---:|"])
    for item in payload["capacity"]["cases"]:
        lines.append(
            f"| {item['initial_cash']} | {item['total_return']} | {item['trade_count']} | "
            f"{item['limit_up_blocked_rate']} | {item['volume_capped_rate']} | {item['cash_blocked_rate']} |"
        )
    lines.extend(["", "## Findings", ""])
    lines.extend(f"- {finding}" for finding in payload["findings"])
    lines.append("")
    return "\n".join(lines)


def run_phase6_stress_audit(
    database: str | Path,
    loop_report_path: str | Path,
    *,
    snapshot_dir: str | Path,
    permutations: int = 20,
    bootstrap_iterations: int = 30,
    date_sample_size: int = 80,
    random_seed: int = 0,
    capacity_levels: Sequence[float] = DEFAULT_CAPACITY_LEVELS,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_ABLATION_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_ABLATION_REPORT_DIR,
) -> dict[str, Any]:
    target = validate_database_path(database)
    loop_report = _load_json(loop_report_path, "research loop report")
    best = _mapping(loop_report.get("best_candidate") or {}, "best_candidate")
    candidate_signature = str(best.get("signature") or "")
    candidate_model = validate_candidate_config(
        _mapping(best.get("model_config") or {}, "best_candidate.model_config")
    )
    backtest_config = normalize_backtest_config(
        _mapping(best.get("backtest_config") or {}, "best_candidate.backtest_config")
    )
    objective = _mapping(loop_report.get("objective") or {}, "loop_report.objective")
    start_date, end_date = _candidate_period(loop_report)
    snapshots = discover_snapshots(snapshot_dir, start_date=start_date, end_date=end_date)
    if not snapshots:
        raise ResearchAblationError("no snapshots were selected")
    codes = {
        str(row.get("code") or "")
        for item in snapshots
        for row in load_feature_snapshot_artifact(str(item["path"])).get("rows") or []
        if isinstance(row, Mapping) and str(row.get("code") or "")
    }
    bars = _filter_bars(load_market_bars(target), codes=codes, end_date=end_date)
    candidate_replay = build_portfolio_replay(snapshots, candidate_model)
    candidate_scores = list(candidate_replay.get("scores") or [])
    exit_ablations = _exit_ablations(
        scores=candidate_scores,
        bars=bars,
        backtest_config=backtest_config,
        objective=objective,
        random_seed=random_seed,
    )
    equal_weights = {feature: 1.0 for feature in FEATURE_NAMES}
    equal_model = validate_candidate_config(
        {
            **candidate_model,
            "version": f"{candidate_model.get('version', 'candidate')}-equal-weight",
            "weights": equal_weights,
            "feature_directions": {feature: 1 for feature in FEATURE_NAMES},
        }
    )
    equal_replay = build_portfolio_replay(snapshots, equal_model)
    equal_variant = _variant_simulation(
        scores=list(equal_replay.get("scores") or []),
        bars=bars,
        base_config=backtest_config,
        objective=objective,
        label="equal_weight_atr_time",
    )
    base_score = float(exit_ablations["variants"][0]["objective"]["score"])
    equal_variant["delta_j_vs_base"] = round(
        float(equal_variant["objective"]["score"]) - base_score, 8
    )
    exit_ablations["variants"].append(equal_variant)
    base_simulation = simulate_trial(
        scores=candidate_scores,
        bars=bars,
        config=backtest_config,
    )
    attribution = _factor_attribution(
        trades=base_simulation["trades"], score_rows=candidate_scores
    )
    development_start = str(
        _mapping(
            _mapping(loop_report.get("walk_forward") or {}, "walk_forward").get(
                "holdout"
            )
            or {},
            "walk_forward.holdout",
        ).get("train_start")
        or start_date
    )
    development_end = str(
        _mapping(
            _mapping(loop_report.get("walk_forward") or {}, "walk_forward").get(
                "holdout"
            )
            or {},
            "walk_forward.holdout",
        ).get("train_end")
        or end_date
    )
    development_snapshots = discover_snapshots(
        snapshot_dir,
        start_date=development_start,
        end_date=development_end,
    )
    statistical_stress = _permutation_and_bootstrap_audit(
        development_snapshots,
        permutations=int(permutations),
        bootstrap_iterations=int(bootstrap_iterations),
        date_sample_size=int(date_sample_size),
        seed=int(random_seed),
    )
    capacity = _capacity_audit(
        scores=candidate_scores,
        bars=bars,
        backtest_config=backtest_config,
        capacity_levels=capacity_levels,
    )
    random_variant = next(
        item
        for item in exit_ablations["variants"]
        if item["label"] == "random_signal_atr_time"
    )
    no_exit_variant = next(
        item for item in exit_ablations["variants"] if item["label"] == "no_active_exit"
    )
    gap = attribution["factors"].get("gap", {})
    findings = [
        f"random-signal delta J: {random_variant['delta_j_vs_base']}",
        f"no-active-exit delta J: {no_exit_variant['delta_j_vs_base']}",
        f"gap share of net PnL: {gap.get('share_of_net_pnl')}",
        f"gap share of turnover: {gap.get('share_of_turnover')}",
        f"gap permutation p-value: {statistical_stress['permutation']['gap']['two_sided_p_value']}",
    ]
    identity = {
        "loop_id": str(loop_report.get("loop_id") or ""),
        "candidate_signature": candidate_signature,
        "candidate_model": candidate_model,
        "backtest_config": backtest_config,
        "period": {"start": start_date, "end": end_date},
        "exit_ablations": exit_ablations,
        "attribution": attribution,
        "statistical_stress": statistical_stress,
        "capacity": capacity,
    }
    audit_id = sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))
    payload = {
        "artifact_type": "kinace_v3_phase6_stress_attribution",
        "schema_version": SCHEMA_VERSION,
        "audit_id": audit_id,
        "status": "completed",
        "loop_id": str(loop_report.get("loop_id") or ""),
        "candidate_signature": candidate_signature,
        "period": {"start": start_date, "end": end_date},
        "settings": {
            "permutations": int(permutations),
            "bootstrap_iterations": int(bootstrap_iterations),
            "date_sample_size": int(date_sample_size),
            "random_seed": int(random_seed),
            "capacity_levels": [float(value) for value in capacity_levels],
        },
        "exit_ablations": exit_ablations,
        "factor_attribution": attribution,
        "statistical_stress": statistical_stress,
        "capacity": capacity,
        "findings": findings,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False) / f"stress-audit-{audit_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False) / f"stress-audit-{audit_id}.md"
    )
    payload["source_loop_report"] = str(Path(loop_report_path).resolve(strict=False))
    payload["snapshot_dir"] = str(Path(snapshot_dir).resolve(strict=False))
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    return payload


__all__ = [
    "DEFAULT_ABLATION_OUTPUT_DIR",
    "DEFAULT_ABLATION_REPORT_DIR",
    "DEFAULT_CAPACITY_LEVELS",
    "ResearchAblationError",
    "SCHEMA_VERSION",
    "run_phase6_stress_audit",
]
