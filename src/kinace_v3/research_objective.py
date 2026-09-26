"""Phase 3 objective rewrite and read-only redundancy diagnostics."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .optimization import (
    DEFAULT_OBJECTIVE,
    PRIMARY_METRIC,
    PRIMARY_SOURCE_METRIC,
    normalize_objective_config,
    score_portfolio_objective,
)


SCHEMA_VERSION = 1
DEFAULT_OBJECTIVE_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_objective"
DEFAULT_OBJECTIVE_REPORT_DIR = PROJECT_ROOT / "reports" / "research_objective"
LEGACY_METRIC_FIELDS = (
    "total_return",
    "annualized_return",
    "excess_return",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "profit_factor",
    "win_rate",
    "turnover",
)


class ResearchObjectiveError(ValueError):
    """Raised when the Phase 3 objective audit cannot be produced safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchObjectiveError(f"{field} must be an object")
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchObjectiveError(f"cannot load {field}: {exc}") from exc


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


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
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


def _derived_metrics(metrics: Mapping[str, Any]) -> dict[str, float | None]:
    result = {field: _number(metrics.get(field)) for field in LEGACY_METRIC_FIELDS}
    if result["calmar"] is None:
        total_return = result["total_return"]
        max_drawdown = result["max_drawdown"]
        if total_return is not None and max_drawdown is not None:
            result["calmar"] = min(
                2.0,
                max(
                    -2.0,
                    total_return / max_drawdown if max_drawdown > 0 else 2.0,
                ),
            )
    return result


def _development_rows(loop_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for round_payload in loop_report.get("rounds") or []:
        if not isinstance(round_payload, Mapping):
            continue
        round_number = int(round_payload.get("round") or 0)
        for candidate in round_payload.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            signature = str(candidate.get("signature") or "")
            for fold in candidate.get("folds") or []:
                if not isinstance(fold, Mapping):
                    continue
                metrics = fold.get("metrics")
                if not isinstance(metrics, Mapping):
                    continue
                rows.append(
                    {
                        "round": round_number,
                        "candidate_signature": signature,
                        "fold": int(fold.get("fold") or 0),
                        "metrics": dict(metrics),
                        "derived": _derived_metrics(metrics),
                        "components": dict(fold.get("components") or {}),
                    }
                )
    if not rows:
        raise ResearchObjectiveError(
            "research loop report has no development-fold metrics"
        )
    return rows


def _legacy_objective(loop_report: Mapping[str, Any]) -> dict[str, Any]:
    objective = loop_report.get("objective")
    return dict(objective) if isinstance(objective, Mapping) else {}


def _correlation_diagnostics(
    rows: Sequence[Mapping[str, Any]],
    legacy_objective: Mapping[str, Any],
) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(LEGACY_METRIC_FIELDS):
        for right in LEGACY_METRIC_FIELDS[left_index + 1 :]:
            left_values: list[float] = []
            right_values: list[float] = []
            for row in rows:
                derived = _mapping(row.get("derived") or {}, "derived metrics")
                left_value = _number(derived.get(left))
                right_value = _number(derived.get(right))
                if left_value is None or right_value is None:
                    continue
                left_values.append(left_value)
                right_values.append(right_value)
            correlation = _spearman(left_values, right_values)
            pairs.append(
                {
                    "left": left,
                    "right": right,
                    "observations": len(left_values),
                    "spearman": round(correlation, 8)
                    if correlation is not None
                    else None,
                }
            )
    redundant_pairs = [
        pair
        for pair in pairs
        if pair["spearman"] is not None and abs(float(pair["spearman"])) >= 0.95
    ]
    component_rows = [
        _mapping(row.get("components") or {}, "components")
        for row in rows
        if row.get("components")
    ]
    component_pairs: list[dict[str, Any]] = []
    component_names = sorted(
        {
            str(name)
            for item in component_rows
            for name in item
            if _number(item.get(name)) is not None
        }
    )
    for left_index, left in enumerate(component_names):
        for right in component_names[left_index + 1 :]:
            left_values = [
                float(item[left])
                for item in component_rows
                if _number(item.get(left)) is not None
                and _number(item.get(right)) is not None
            ]
            right_values = [
                float(item[right])
                for item in component_rows
                if _number(item.get(left)) is not None
                and _number(item.get(right)) is not None
            ]
            correlation = _spearman(left_values, right_values)
            component_pairs.append(
                {
                    "left": left,
                    "right": right,
                    "observations": len(left_values),
                    "spearman": round(correlation, 8)
                    if correlation is not None
                    else None,
                }
            )
    component_redundancy = [
        pair
        for pair in component_pairs
        if pair["spearman"] is not None and abs(float(pair["spearman"])) >= 0.95
    ]
    return {
        "development_observations": len(rows),
        "metric_rank_correlations": pairs,
        "high_redundancy_pairs": redundant_pairs,
        "legacy_component_rank_correlations": component_pairs,
        "high_redundancy_components": component_redundancy,
        "legacy_weights": dict(_mapping(legacy_objective.get("weights") or {}, "legacy weights")),
    }


def _max_weight_report(
    loop_report: Mapping[str, Any],
    *,
    budget: float,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for round_payload in loop_report.get("rounds") or []:
        if not isinstance(round_payload, Mapping):
            continue
        round_number = int(round_payload.get("round") or 0)
        for candidate in round_payload.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            signature = str(candidate.get("signature") or "")
            sources: list[tuple[str, Mapping[str, Any]]] = []
            final_training = candidate.get("final_training")
            if isinstance(final_training, Mapping) and isinstance(
                final_training.get("trained_weights"), Mapping
            ):
                sources.append(("final_training", final_training["trained_weights"]))
            for fold in candidate.get("folds") or []:
                if not isinstance(fold, Mapping):
                    continue
                training = fold.get("training")
                if isinstance(training, Mapping) and isinstance(
                    training.get("trained_weights"), Mapping
                ):
                    sources.append(
                        (
                            f"fold_{int(fold.get('fold') or 0)}",
                            training["trained_weights"],
                        )
                    )
            for source, weights in sources:
                values = {
                    str(name): float(value)
                    for name, value in weights.items()
                    if _number(value) is not None
                }
                if not values:
                    continue
                maximum = max(values.values())
                dominant = sorted(
                    values.items(), key=lambda item: (-item[1], item[0])
                )[0][0]
                observations.append(
                    {
                        "round": round_number,
                        "candidate_signature": signature,
                        "source": source,
                        "max_feature_weight": round(maximum, 8),
                        "dominant_feature": dominant,
                        "exceeds_budget": maximum > budget,
                        "weights": values,
                    }
                )
    maxima = [float(item["max_feature_weight"]) for item in observations]

    def percentile(values: Sequence[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[position]

    dominant_counts = Counter(str(item["dominant_feature"]) for item in observations)
    return {
        "binding": False,
        "status": "report_only",
        "budget": float(budget),
        "vector_count": len(observations),
        "exceeding_count": sum(bool(item["exceeds_budget"]) for item in observations),
        "exceeding_rate": round(
            sum(bool(item["exceeds_budget"]) for item in observations)
            / len(observations),
            8,
        )
        if observations
        else 0.0,
        "min": round(min(maxima), 8) if maxima else None,
        "median": round(statistics.median(maxima), 8) if maxima else None,
        "p95": round(float(percentile(maxima, 0.95) or 0.0), 8)
        if maxima
        else None,
        "max": round(max(maxima), 8) if maxima else None,
        "dominant_feature_counts": dict(sorted(dominant_counts.items())),
        "observations": observations,
    }


def _evaluate_windows(
    loop_report: Mapping[str, Any],
    *,
    objective: Mapping[str, Any],
) -> dict[str, Any]:
    best = _mapping(loop_report.get("best_candidate") or {}, "best_candidate")
    development = []
    for fold in best.get("folds") or []:
        if not isinstance(fold, Mapping):
            continue
        metrics = fold.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        development.append(
            {
                "kind": "walk_forward_fold",
                "fold": int(fold.get("fold") or 0),
                "metrics": dict(metrics),
                "evaluation": score_portfolio_objective(metrics, objective),
            }
        )
    holdout = loop_report.get("holdout")
    holdout_evaluation = None
    if isinstance(holdout, Mapping) and isinstance(holdout.get("metrics"), Mapping):
        holdout_evaluation = {
            "kind": "final_holdout",
            "metrics": dict(holdout["metrics"]),
            "evaluation": score_portfolio_objective(holdout["metrics"], objective),
        }
    return {
        "development_folds": development,
        "holdout_after_policy_freeze": holdout_evaluation,
    }


def build_objective_policy_audit(
    loop_report: Mapping[str, Any],
    *,
    objective_config: Mapping[str, Any] | None = None,
    max_feature_weight_budget: float | None = None,
) -> dict[str, Any]:
    """Build the Phase 3 policy artifact from an existing research-loop report."""
    loop_report = _mapping(loop_report, "research loop report")
    objective = normalize_objective_config(objective_config or DEFAULT_OBJECTIVE)
    if max_feature_weight_budget is not None:
        objective["report_only"]["max_feature_weight"]["threshold"] = float(
            max_feature_weight_budget
        )
        objective = normalize_objective_config(objective)
    development_rows = _development_rows(loop_report)
    legacy_objective = _legacy_objective(loop_report)
    correlations = _correlation_diagnostics(development_rows, legacy_objective)
    max_weight = _max_weight_report(
        loop_report,
        budget=float(objective["report_only"]["max_feature_weight"]["threshold"]),
    )
    evaluations = _evaluate_windows(loop_report, objective=objective)
    loop_id = str(loop_report.get("loop_id") or "")
    best = _mapping(loop_report.get("best_candidate") or {}, "best_candidate")
    candidate_signature = str(best.get("signature") or "")
    if not loop_id or not candidate_signature:
        raise ResearchObjectiveError(
            "research loop report is missing loop or candidate identity"
        )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "loop_id": loop_id,
        "candidate_signature": candidate_signature,
        "objective": objective,
        "development_observation_hash": sha256_bytes(
            canonical_json_bytes(development_rows, trailing_newline=False)
        ),
        "max_weight_report_hash": sha256_bytes(
            canonical_json_bytes(max_weight, trailing_newline=False)
        ),
    }
    policy_id = sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))
    primary = objective["primary"]
    findings = [
        "primary objective is net_sharpe from the cost-inclusive portfolio equity curve",
        "risk and execution limits are binding constraints; they are not weighted reward terms",
        "max_feature_weight remains report-only until Phase 4 supplies projection",
        f"development observations: {len(development_rows)}",
        f"high-redundancy metric pairs: {len(correlations['high_redundancy_pairs'])}",
        f"max_feature_weight vectors: {max_weight['vector_count']}",
    ]
    return {
        "artifact_type": "kinace_v3_phase3_objective_policy",
        "schema_version": SCHEMA_VERSION,
        "policy_id": policy_id,
        "status": "completed",
        "phase": 3,
        "loop_id": loop_id,
        "candidate_signature": candidate_signature,
        "objective": objective,
        "objective_policy_identity": identity,
        "selection_policy": {
            "selected_on": "development_walk_forward_only",
            "holdout_used_for_selection": False,
            "source": objective["policy"]["source"],
            "version": objective["policy"]["version"],
        },
        "correlation_diagnostics": correlations,
        "max_weight_report_only": max_weight,
        "evaluation": evaluations,
        "invalidates_prior_objective": True,
        "candidate_requalification_required": True,
        "requires_phase4_weight_solver_before_max_weight_binding": True,
        "findings": findings,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }


def initialize_objective_schema(database: str | Path) -> Path:
    target = validate_database_path(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_objective_policy_runs (
                policy_id TEXT PRIMARY KEY,
                loop_id TEXT NOT NULL,
                candidate_signature TEXT NOT NULL,
                status TEXT NOT NULL,
                primary_metric TEXT NOT NULL,
                max_feature_weight_binding INTEGER NOT NULL,
                max_feature_weight_budget REAL NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                summary_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_research_objective_policy_loop
                ON research_objective_policy_runs(loop_id, created_at_utc);
            """
        )
    return target


def _markdown_report(payload: Mapping[str, Any]) -> str:
    objective = payload["objective"]
    primary = objective["primary"]
    correlations = payload["correlation_diagnostics"]
    weight_report = payload["max_weight_report_only"]
    lines = [
        "# Kinace Phase 3 Objective Policy",
        "",
        f"- Policy ID: `{payload['policy_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Loop: `{payload['loop_id']}`",
        f"- Candidate: `{payload['candidate_signature']}`",
        f"- Primary metric: `{primary['metric']}`",
        f"- Source metric: `{primary['source_metric']}`",
        f"- Direction: `{primary['direction']}`",
        f"- Normalization: `{primary['normalization']}`",
        f"- Clip: `[{primary['clip_min']}, {primary['clip_max']}]`",
        f"- Policy version: `{objective['policy']['version']}`",
        f"- Holdout used for selection: `{objective['policy']['holdout_used_for_selection']}`",
        "",
        "## Binding Constraints",
        "",
        "| Constraint | Operator | Threshold | Source |",
        "|---|---:|---:|---|",
    ]
    for name, constraint in objective["constraints"].items():
        lines.append(
            f"| `{name}` | `{constraint['operator']}` | {constraint['threshold']} | "
            f"`{constraint['source']}` |"
        )
    max_feature = objective["report_only"]["max_feature_weight"]
    lines.extend(
        [
            "",
            "## Report-Only Constraint",
            "",
            f"- `max_feature_weight`: `{max_feature['operator']} {max_feature['threshold']}`",
            f"- Binding: `{max_feature['binding']}`",
            "",
            "## Redundancy Diagnostics",
            "",
            f"- Development observations: `{correlations['development_observations']}`",
            f"- High-redundancy metric pairs: `{len(correlations['high_redundancy_pairs'])}`",
            "",
            "| Left | Right | Spearman |",
            "|---|---|---:|",
        ]
    )
    for pair in correlations["high_redundancy_pairs"][:20]:
        lines.append(
            f"| `{pair['left']}` | `{pair['right']}` | {pair['spearman']} |"
        )
    lines.extend(
        [
            "",
            "## Max Weight Report",
            "",
            f"- Status: `{weight_report['status']}`",
            f"- Binding: `{weight_report['binding']}`",
            f"- Vectors: `{weight_report['vector_count']}`",
            f"- Exceeded: `{weight_report['exceeding_count']}`",
            f"- Median max weight: `{weight_report['median']}`",
            f"- Maximum max weight: `{weight_report['max']}`",
            "",
            "## Findings",
            "",
        ]
    )
    lines.extend(f"- {finding}" for finding in payload["findings"])
    lines.append("")
    return "\n".join(lines)


def run_objective_policy_audit(
    database: str | Path | None,
    loop_report_path: str | Path,
    *,
    objective_config: Mapping[str, Any] | None = None,
    max_feature_weight_budget: float | None = None,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OBJECTIVE_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_OBJECTIVE_REPORT_DIR,
) -> dict[str, Any]:
    loop_report = _mapping(
        _load_json(loop_report_path, "research loop report"),
        "research loop report",
    )
    payload = build_objective_policy_audit(
        loop_report,
        objective_config=objective_config,
        max_feature_weight_budget=max_feature_weight_budget,
    )
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False)
        / f"objective-policy-{payload['policy_id']}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False)
        / f"objective-policy-{payload['policy_id']}.md"
    )
    payload["source_loop_report"] = str(Path(loop_report_path).resolve(strict=False))
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    if database is not None:
        target = initialize_objective_schema(database)
        with _connect(target) as connection:
            connection.execute(
                """
                INSERT INTO research_objective_policy_runs (
                    policy_id, loop_id, candidate_signature, status,
                    primary_metric, max_feature_weight_binding,
                    max_feature_weight_budget, artifact_path, artifact_sha256,
                    summary_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    status=excluded.status,
                    artifact_path=excluded.artifact_path,
                    artifact_sha256=excluded.artifact_sha256,
                    summary_json=excluded.summary_json,
                    created_at_utc=excluded.created_at_utc
                """,
                (
                    payload["policy_id"],
                    payload["loop_id"],
                    payload["candidate_signature"],
                    payload["status"],
                    PRIMARY_METRIC,
                    0,
                    float(
                        payload["objective"]["report_only"]["max_feature_weight"][
                            "threshold"
                        ]
                    ),
                    str(written),
                    payload["artifact_sha256"],
                    canonical_json_bytes(
                        {
                            "status": payload["status"],
                            "objective": payload["objective"],
                            "high_redundancy_pairs": payload[
                                "correlation_diagnostics"
                            ]["high_redundancy_pairs"],
                            "max_weight_report_only": {
                                key: value
                                for key, value in payload[
                                    "max_weight_report_only"
                                ].items()
                                if key != "observations"
                            },
                        },
                        trailing_newline=False,
                    ).decode("utf-8"),
                    _utc_now(),
                ),
            )
    return payload


__all__ = [
    "DEFAULT_OBJECTIVE_OUTPUT_DIR",
    "DEFAULT_OBJECTIVE_REPORT_DIR",
    "ResearchObjectiveError",
    "SCHEMA_VERSION",
    "build_objective_policy_audit",
    "initialize_objective_schema",
    "run_objective_policy_audit",
]
