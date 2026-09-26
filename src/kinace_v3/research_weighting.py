"""Phase 4 robust factor standardization and constrained ICIR weighting."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT


STANDARDIZATION_VERSION = 1
WEIGHTING_VERSION = "kinace-v3.3-phase4-signed-icir-v1"
DEFAULT_STANDARDIZATION: dict[str, Any] = {
    "version": STANDARDIZATION_VERSION,
    "method": "mad_winsorized",
    "mad_multiplier": 3.0,
    "neutralize_industry": False,
    "neutralize_market_cap": False,
}
DEFAULT_WEIGHTING_POLICY: dict[str, Any] = {
    "version": WEIGHTING_VERSION,
    "method": "signed_icir_constrained_simplex",
    "prior_mix": 0.25,
    "sign_epsilon": 1e-12,
    "minimum_sign_consistency": 0.50,
    "max_feature_weight_binding": True,
}
DEFAULT_WEIGHTING_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_weighting"
DEFAULT_WEIGHTING_REPORT_DIR = PROJECT_ROOT / "reports" / "research_weighting"


class ResearchWeightingError(ValueError):
    """Raised when Phase 4 standardization or weighting is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(payload: Any, *, trailing_newline: bool = False) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if trailing_newline:
        text += "\n"
    return text.encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(_canonical_json_bytes(payload, trailing_newline=True))
    temporary.replace(target)
    return target


def _write_text(text: str, path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def _load_json(path: str | Path, field: str) -> dict[str, Any]:
    target = Path(path).resolve(strict=False)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchWeightingError(f"cannot load {field}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ResearchWeightingError(f"{field} must be an object")
    return dict(payload)


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchWeightingError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchWeightingError(f"{field} must be finite")
    return result


def normalize_standardization_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(config or {})
    unknown = sorted(set(source) - set(DEFAULT_STANDARDIZATION))
    if unknown:
        raise ResearchWeightingError(
            "unknown standardization keys: " + ", ".join(unknown)
        )
    result = dict(DEFAULT_STANDARDIZATION)
    result.update(source)
    if result["version"] != STANDARDIZATION_VERSION:
        raise ResearchWeightingError(
            f"standardization.version must be {STANDARDIZATION_VERSION}"
        )
    if result["method"] not in {"raw", "mad_winsorized"}:
        raise ResearchWeightingError(
            "standardization.method must be raw or mad_winsorized"
        )
    result["mad_multiplier"] = _finite_number(
        result["mad_multiplier"], "standardization.mad_multiplier"
    )
    if result["mad_multiplier"] <= 0:
        raise ResearchWeightingError(
            "standardization.mad_multiplier must be positive"
        )
    for key in ("neutralize_industry", "neutralize_market_cap"):
        if not isinstance(result[key], bool):
            raise ResearchWeightingError(f"standardization.{key} must be boolean")
    return result


def _median(values: Sequence[float]) -> float:
    return statistics.median(values)


def winsorize_mad(values: Sequence[float], multiplier: float = 3.0) -> list[float]:
    """Clamp values to median +/- multiplier * scaled MAD."""
    clean = [_finite_number(value, "standardization value") for value in values]
    if not clean:
        return []
    center = _median(clean)
    mad = _median([abs(value - center) for value in clean])
    if mad <= 0:
        return clean
    scale = 1.4826 * mad
    low = center - multiplier * scale
    high = center + multiplier * scale
    return [min(high, max(low, value)) for value in clean]


def neutralize_by_group(
    values: Sequence[float],
    groups: Sequence[str],
) -> list[float]:
    if len(values) != len(groups):
        raise ResearchWeightingError("values and groups must have equal length")
    grouped: dict[str, list[float]] = defaultdict(list)
    for value, group in zip(values, groups):
        grouped[str(group)].append(float(value))
    means = {name: statistics.fmean(items) for name, items in grouped.items()}
    return [float(value) - means[str(group)] for value, group in zip(values, groups)]


def neutralize_by_industry_market_cap(
    values: Sequence[float],
    industries: Sequence[str],
    market_caps: Sequence[float],
) -> list[float]:
    if not (len(values) == len(industries) == len(market_caps)):
        raise ResearchWeightingError(
            "values, industries, and market_caps must have equal length"
        )
    industry_residual = neutralize_by_group(values, industries)
    log_caps = [
        math.log(value) if float(value) > 0 else 0.0 for value in market_caps
    ]
    cap_residual = neutralize_by_group(log_caps, industries)
    denominator = sum(value * value for value in cap_residual)
    if denominator <= 1e-18:
        return industry_residual
    beta = sum(
        cap_value * factor_value
        for cap_value, factor_value in zip(cap_residual, industry_residual)
    ) / denominator
    return [
        factor_value - beta * cap_value
        for factor_value, cap_value in zip(industry_residual, cap_residual)
    ]


def standardize_values(
    values: Sequence[float],
    *,
    config: Mapping[str, Any] | None = None,
    industries: Sequence[str] | None = None,
    market_caps: Sequence[float] | None = None,
) -> list[float]:
    """Winsorize, optionally neutralize, then z-score one cross-section."""
    settings = normalize_standardization_config(config)
    clean = [_finite_number(value, "standardization value") for value in values]
    if not clean:
        return []
    if settings["method"] == "mad_winsorized":
        clean = winsorize_mad(clean, float(settings["mad_multiplier"]))
    if settings["neutralize_market_cap"]:
        if market_caps is None:
            raise ResearchWeightingError(
                "market_caps are required for market-cap neutralization"
            )
        if len(clean) != len(market_caps):
            raise ResearchWeightingError(
                "market_caps must align with standardization values"
            )
        aligned_industries = list(industries or ["__all__"] * len(clean))
        clean = neutralize_by_industry_market_cap(
            clean, aligned_industries, market_caps
        )
    elif settings["neutralize_industry"] and industries is not None:
        if len(clean) != len(industries):
            raise ResearchWeightingError(
                "industries must align with standardization values"
            )
        clean = neutralize_by_group(clean, industries)
    mean = statistics.fmean(clean)
    deviation = statistics.stdev(clean) if len(clean) >= 2 else 0.0
    if deviation <= 0:
        return [0.0 for _ in clean]
    return [(value - mean) / deviation for value in clean]


def _autocovariance(values: Sequence[float], lag: int) -> float:
    if lag <= 0 or lag >= len(values):
        return sum(value * value for value in values) / len(values)
    return sum(
        values[index] * values[index - lag] for index in range(lag, len(values))
    ) / len(values)


def summarize_ic_series(
    series: Sequence[float],
    *,
    sign_epsilon: float = 1e-12,
    recency_halflife: float | None = None,
    recency_mode: str = "direction_and_strength",
    conflict_scale: float = 0.25,
) -> dict[str, Any]:
    """Summarize daily Rank IC with sign stability and Newey-West inference."""
    values = [_finite_number(value, "IC value") for value in series]
    observations = len(values)
    if not values:
        return {
            "observations": 0,
            "mean_ic": 0.0,
            "stdev_ic": 0.0,
            "icir": 0.0,
            "sign_consistency": 0.0,
            "positive_rate": 0.0,
            "negative_rate": 0.0,
            "rho_1": 0.0,
            "newey_west_t_stat": 0.0,
            "direction": 1,
            "strength": 0.0,
            "direction_basis": "full_sample",
            "strength_basis": "full_sample",
        }
    mean = statistics.fmean(values)
    deviation = statistics.stdev(values) if observations >= 2 else 0.0
    icir = mean / deviation if deviation > 0 else 0.0
    centered = [value - mean for value in values]
    rho_1 = (
        _autocovariance(centered, 1) / _autocovariance(centered, 0)
        if observations >= 3 and _autocovariance(centered, 0) > 0
        else 0.0
    )
    default_truncation = int(
        math.floor(4.0 * (observations / 100.0) ** (2.0 / 9.0))
    )
    truncation = max(1, min(observations - 1, default_truncation))
    long_run = _autocovariance(centered, 0)
    for lag in range(1, truncation + 1):
        weight = 1.0 - lag / (truncation + 1.0)
        long_run += 2.0 * weight * _autocovariance(centered, lag)
    long_run = max(long_run, 1e-18)
    newey_west_se = math.sqrt(long_run / observations)
    newey_west_t = mean / newey_west_se if newey_west_se > 0 else 0.0
    epsilon = _finite_number(sign_epsilon, "sign_epsilon")
    nonzero = [value for value in values if abs(value) > epsilon]
    direction = 1 if mean > epsilon else -1 if mean < -epsilon else 1
    sign_consistency = (
        sum((value > 0) == (direction > 0) for value in nonzero) / len(nonzero)
        if nonzero
        else 0.0
    )
    positive_rate = sum(value > epsilon for value in values) / observations
    negative_rate = sum(value < -epsilon for value in values) / observations
    strength = abs(icir) * sign_consistency
    result = {
        "observations": observations,
        "mean_ic": mean,
        "stdev_ic": deviation,
        "icir": icir,
        "sign_consistency": sign_consistency,
        "positive_rate": positive_rate,
        "negative_rate": negative_rate,
        "rho_1": rho_1,
        "newey_west_t_stat": newey_west_t,
        "direction": direction,
        "strength": strength,
        "direction_basis": "full_sample",
        "strength_basis": "full_sample",
    }
    if recency_halflife is None:
        return result
    half_life = _finite_number(recency_halflife, "recency_halflife")
    if half_life <= 0:
        raise ResearchWeightingError("recency_halflife must be positive")
    if recency_mode not in {
        "direction_and_strength",
        "direction_only",
        "conflict_contract",
    }:
        raise ResearchWeightingError(
            "recency_mode must be direction_and_strength, direction_only, or conflict_contract"
        )
    contraction = _finite_number(conflict_scale, "conflict_scale")
    if not 0 <= contraction <= 1:
        raise ResearchWeightingError("conflict_scale must be in [0, 1]")
    weights = [
        0.5 ** ((observations - 1 - index) / half_life)
        for index in range(observations)
    ]
    total_weight = sum(weights)
    weighted_mean = sum(
        weight * value for weight, value in zip(weights, values)
    ) / total_weight
    weighted_deviation = 0.0
    if observations >= 2:
        denominator = total_weight - sum(weight * weight for weight in weights) / total_weight
        if denominator > 0:
            weighted_variance = sum(
                weight * (value - weighted_mean) ** 2
                for weight, value in zip(weights, values)
            ) / denominator
            weighted_deviation = math.sqrt(max(weighted_variance, 0.0))
    weighted_icir = weighted_mean / weighted_deviation if weighted_deviation > 0 else 0.0
    weighted_direction = (
        1 if weighted_mean > epsilon else -1 if weighted_mean < -epsilon else 1
    )
    weighted_nonzero = [
        (weight, value)
        for weight, value in zip(weights, values)
        if abs(value) > epsilon
    ]
    weighted_sign_consistency = (
        sum(
            weight
            for weight, value in weighted_nonzero
            if (value > 0) == (weighted_direction > 0)
        )
        / sum(weight for weight, _ in weighted_nonzero)
        if weighted_nonzero
        else 0.0
    )
    if recency_mode == "conflict_contract":
        conflict_detected = direction != weighted_direction
        result["direction"] = direction
        result["direction_basis"] = "full_sample"
        result["strength"] = strength * (contraction if conflict_detected else 1.0)
        result["strength_basis"] = (
            "conflict_contracted" if conflict_detected else "full_sample"
        )
        result["conflict_contract"] = {
            "detected": conflict_detected,
            "scale": contraction,
            "full_sample_direction": direction,
            "recency_direction": weighted_direction,
        }
    elif recency_mode == "direction_only":
        result["direction"] = weighted_direction
        result["direction_basis"] = "recency_weighted"
        result["strength_basis"] = "full_sample"
    else:
        result["direction"] = weighted_direction
        result["direction_basis"] = "recency_weighted"
        result["strength"] = abs(weighted_icir) * weighted_sign_consistency
        result["strength_basis"] = "recency_weighted"
    result["recency_weighted"] = {
        "halflife_sessions": half_life,
        "mean_ic": weighted_mean,
        "stdev_ic": weighted_deviation,
        "icir": weighted_icir,
        "sign_consistency": weighted_sign_consistency,
    }
    return result


def project_onto_constrained_simplex(
    values: Sequence[float],
    max_weight: float,
) -> list[float]:
    """Project values onto sum=1, weight>=0, weight<=max_weight."""
    clean = [_finite_number(value, "raw weight") for value in values]
    if not clean:
        raise ResearchWeightingError("raw weights must not be empty")
    maximum = _finite_number(max_weight, "max_weight")
    if maximum <= 0 or maximum > 1:
        raise ResearchWeightingError("max_weight must be in (0, 1]")
    if maximum * len(clean) < 1.0 - 1e-15:
        raise ResearchWeightingError("max_weight makes the constrained simplex infeasible")
    low = min(clean) - maximum
    high = max(clean)
    for _ in range(200):
        middle = (low + high) / 2.0
        projected = [
            min(maximum, max(0.0, value - middle)) for value in clean
        ]
        if sum(projected) > 1.0:
            low = middle
        else:
            high = middle
    theta = (low + high) / 2.0
    projected = [min(maximum, max(0.0, value - theta)) for value in clean]
    residual = 1.0 - sum(projected)
    for index in range(len(projected)):
        if abs(residual) <= 1e-15:
            break
        if residual > 0:
            capacity = maximum - projected[index]
            adjustment = min(capacity, residual)
            projected[index] += adjustment
            residual -= adjustment
        else:
            adjustment = min(projected[index], -residual)
            projected[index] -= adjustment
            residual += adjustment
    return [round(value, 12) for value in projected]


def fit_signed_icir_weights(
    *,
    base_weights: Mapping[str, float],
    ic_statistics: Mapping[str, Mapping[str, Any]],
    max_feature_weight: float,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit long-only projected weights while preserving factor directions."""
    settings = dict(DEFAULT_WEIGHTING_POLICY)
    settings.update(policy or {})
    if settings["method"] != "signed_icir_constrained_simplex":
        raise ResearchWeightingError("unsupported signed ICIR weighting method")
    names = sorted(str(name) for name in base_weights)
    if not names:
        raise ResearchWeightingError("base_weights must not be empty")
    prior_mix = _finite_number(settings["prior_mix"], "policy.prior_mix")
    if not 0 <= prior_mix <= 1:
        raise ResearchWeightingError("policy.prior_mix must be in [0, 1]")
    minimum_sign_consistency = _finite_number(
        settings["minimum_sign_consistency"],
        "policy.minimum_sign_consistency",
    )
    if not 0 <= minimum_sign_consistency <= 1:
        raise ResearchWeightingError(
            "policy.minimum_sign_consistency must be in [0, 1]"
        )
    base = {name: max(0.0, _finite_number(base_weights[name], name)) for name in names}
    base_sum = sum(base.values())
    if base_sum <= 0:
        raise ResearchWeightingError("base_weights must have a positive sum")
    base_normalized = {name: base[name] / base_sum for name in names}
    statistics_by_name = {
        name: dict(ic_statistics.get(name) or {}) for name in names
    }
    directions = {
        name: int(statistics_by_name[name].get("direction") or 1) for name in names
    }
    strengths = {}
    for name in names:
        statistics_value = statistics_by_name[name]
        consistency = float(statistics_value.get("sign_consistency") or 0.0)
        strengths[name] = (
            max(0.0, float(statistics_value.get("strength") or 0.0))
            if consistency >= minimum_sign_consistency
            else 0.0
        )
    strength_sum = sum(strengths.values())
    if strength_sum <= 1e-18:
        learned = dict(base_normalized)
        fallback = True
    else:
        learned = {name: strengths[name] / strength_sum for name in names}
        fallback = False
    raw = {
        name: prior_mix * base_normalized[name]
        + (1.0 - prior_mix) * learned[name]
        for name in names
    }
    projected_values = project_onto_constrained_simplex(
        [raw[name] for name in names], max_feature_weight
    )
    weights = dict(zip(names, projected_values))
    maximum = max(weights.values()) if weights else 0.0
    return {
        "version": str(settings["version"]),
        "method": str(settings["method"]),
        "fallback_to_base_weights": fallback,
        "max_feature_weight_budget": float(max_feature_weight),
        "max_feature_weight": maximum,
        "max_feature_weight_satisfied": maximum <= max_feature_weight + 1e-12,
        "directions": directions,
        "ic_statistics": statistics_by_name,
        "base_weights": base_normalized,
        "learned_weights": learned,
        "raw_weights": raw,
        "trained_weights": weights,
        "feature_directions": directions,
    }


def _standardization_evidence(
    phase05_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not phase05_report:
        return {
            "status": "not_supplied",
            "classification_counts": {},
            "factor_sign_consistency": {},
            "findings": [],
        }
    factors = []
    sign_consistency: dict[str, float] = {}
    for item in phase05_report.get("factors") or []:
        if not isinstance(item, Mapping):
            continue
        factor = str(item.get("factor") or "")
        if factor:
            sign_consistency[factor] = float(
                item.get("all_method_sign_consistency") or 0.0
            )
        factors.append(
            {
                "factor": factor,
                "classification": str(item.get("classification") or ""),
                "recommendation": str(item.get("recommendation") or ""),
                "all_method_sign_consistency": float(
                    item.get("all_method_sign_consistency") or 0.0
                ),
            }
        )
    return {
        "status": str(phase05_report.get("status") or "unknown"),
        "classification_counts": deepcopy(
            phase05_report.get("classifications") or {}
        ),
        "factor_sign_consistency": sign_consistency,
        "factors": factors,
        "findings": [str(value) for value in phase05_report.get("findings") or []],
    }


def _legacy_weighting_evidence(
    loop_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    if loop_report:
        for round_payload in loop_report.get("rounds") or []:
            if not isinstance(round_payload, Mapping):
                continue
            for candidate in round_payload.get("candidates") or []:
                if not isinstance(candidate, Mapping):
                    continue
                signature = str(candidate.get("signature") or "")
                for fold in candidate.get("folds") or []:
                    if not isinstance(fold, Mapping):
                        continue
                    training = fold.get("training")
                    if not isinstance(training, Mapping):
                        continue
                    mean_ic = training.get("mean_rank_ic")
                    weights = training.get("trained_weights")
                    if not isinstance(mean_ic, Mapping) or not isinstance(
                        weights, Mapping
                    ):
                        continue
                    negative = {
                        str(name): float(value)
                        for name, value in mean_ic.items()
                        if isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and float(value) < 0
                    }
                    observations.append(
                        {
                            "candidate_signature": signature,
                            "fold": int(fold.get("fold") or 0),
                            "negative_mean_ic": negative,
                            "old_weights_for_negative_ic": {
                                name: float(weights.get(name) or 0.0)
                                for name in negative
                            },
                            "old_weights_ignored_negative_direction": bool(negative),
                        }
                    )
    return {
        "training_observations": len(observations),
        "observations": observations,
        "negative_direction_handling_changed": bool(observations),
    }


def _solver_verification(max_feature_weight: float) -> dict[str, Any]:
    fitted = fit_signed_icir_weights(
        base_weights={"alpha": 0.5, "beta": 0.3, "gamma": 0.2},
        ic_statistics={
            "alpha": summarize_ic_series([0.08, 0.10, 0.09, 0.11]),
            "beta": summarize_ic_series([-0.09, -0.10, -0.08, -0.11]),
            "gamma": summarize_ic_series([0.0, 0.0, 0.0, 0.0]),
        },
        max_feature_weight=max_feature_weight,
    )
    projected = project_onto_constrained_simplex([0.9, 0.1, 0.0, 0.0], max_feature_weight)
    checks = {
        "negative_ic_direction_preserved": fitted["feature_directions"]["beta"] == -1,
        "negative_ic_weight_nonzero": fitted["trained_weights"]["beta"] > 0,
        "weights_sum_to_one": abs(sum(fitted["trained_weights"].values()) - 1.0)
        <= 1e-9,
        "solver_satisfies_max_weight": fitted["max_feature_weight_satisfied"],
        "projection_satisfies_max_weight": max(projected) <= max_feature_weight + 1e-12,
    }
    return {
        "fixture": "deterministic_signed_icir_fixture",
        "checks": checks,
        "passed": all(checks.values()),
        "example": fitted,
    }


def _markdown_report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Kinace Phase 4 Weighting Policy",
        "",
        f"- Policy ID: `{payload['policy_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Standardization: `{payload['standardization']['method']}`",
        f"- MAD multiplier: `{payload['standardization']['mad_multiplier']}`",
        f"- Weighting method: `{payload['weighting']['method']}`",
        f"- Max feature weight: `{payload['weighting']['max_feature_weight_budget']}`",
        f"- Solver verification: `{payload['solver_verification']['passed']}`",
        f"- Data-dependent rerun required: `{payload['data_dependent_rerun_required']}`",
        "",
        "## Solver Checks",
        "",
        "| Check | Passed |",
        "|---|---:|",
    ]
    for key, value in payload["solver_verification"]["checks"].items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload["notes"])
    lines.append("")
    return "\n".join(lines)


def build_phase4_policy_audit(
    *,
    phase05_report: Mapping[str, Any] | None = None,
    loop_report: Mapping[str, Any] | None = None,
    max_feature_weight: float = 0.75,
) -> dict[str, Any]:
    maximum = _finite_number(max_feature_weight, "max_feature_weight")
    standardization = normalize_standardization_config()
    evidence = _standardization_evidence(phase05_report)
    legacy = _legacy_weighting_evidence(loop_report)
    verification = _solver_verification(maximum)
    identity = {
        "standardization": standardization,
        "weighting": DEFAULT_WEIGHTING_POLICY,
        "max_feature_weight": maximum,
        "phase05_hash": _sha256_bytes(
            _canonical_json_bytes(evidence)
        ),
        "legacy_hash": _sha256_bytes(_canonical_json_bytes(legacy)),
    }
    policy_id = _sha256_bytes(_canonical_json_bytes(identity))
    notes = [
        "MAD winsorization is selected from the Phase 0.5 sign-consistency diagnostic.",
        "Industry and market-cap neutralization remain explicit config options rather than forced defaults.",
        "Signed ICIR directions preserve negative predictor information without allowing negative long-only weights.",
        "Constrained-simplex projection enforces non-negative weights, sum one, and max_feature_weight.",
        "A candidate rerun on the Phase 2 snapshot set is still required before new promotion evidence exists.",
    ]
    return {
        "artifact_type": "kinace_v3_phase4_weighting_policy",
        "schema_version": 1,
        "policy_id": policy_id,
        "status": "completed_method",
        "standardization": standardization,
        "weighting": {
            **DEFAULT_WEIGHTING_POLICY,
            "max_feature_weight_budget": maximum,
        },
        "phase05_evidence": evidence,
        "legacy_candidate_evidence": legacy,
        "solver_verification": verification,
        "data_dependent_rerun_required": True,
        "invalidates_prior_candidate_evidence": True,
        "notes": notes,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }


def run_phase4_policy_audit(
    *,
    phase05_report_path: str | Path,
    loop_report_path: str | Path | None = None,
    max_feature_weight: float = 0.75,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_WEIGHTING_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_WEIGHTING_REPORT_DIR,
) -> dict[str, Any]:
    payload = build_phase4_policy_audit(
        phase05_report=_load_json(phase05_report_path, "Phase 0.5 report"),
        loop_report=(
            _load_json(loop_report_path, "research loop report")
            if loop_report_path is not None
            else None
        ),
        max_feature_weight=max_feature_weight,
    )
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False)
        / f"weighting-policy-{payload['policy_id']}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False)
        / f"weighting-policy-{payload['policy_id']}.md"
    )
    payload["source_phase05_report"] = str(
        Path(phase05_report_path).resolve(strict=False)
    )
    payload["source_loop_report"] = (
        str(Path(loop_report_path).resolve(strict=False))
        if loop_report_path is not None
        else None
    )
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = _sha256_bytes(written.read_bytes())
    return payload


__all__ = [
    "DEFAULT_STANDARDIZATION",
    "DEFAULT_WEIGHTING_POLICY",
    "DEFAULT_WEIGHTING_OUTPUT_DIR",
    "DEFAULT_WEIGHTING_REPORT_DIR",
    "ResearchWeightingError",
    "STANDARDIZATION_VERSION",
    "WEIGHTING_VERSION",
    "build_phase4_policy_audit",
    "fit_signed_icir_weights",
    "neutralize_by_group",
    "neutralize_by_industry_market_cap",
    "normalize_standardization_config",
    "project_onto_constrained_simplex",
    "run_phase4_policy_audit",
    "standardize_values",
    "summarize_ic_series",
    "winsorize_mad",
]
