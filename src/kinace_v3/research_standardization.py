"""Read-only IC diagnostics for winsorization and neutralization choices."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .data_collection.models import RevisionStatus
from .data_collection.store import RevisionStore
from .feature_snapshot import (
    FEATURE_NAMES,
    canonical_json_bytes,
    load_feature_snapshot_artifact,
    sha256_bytes,
    sha256_file,
)
from .portfolio_backtest import discover_snapshots
from .research_diagnose import load_industry_map


SCHEMA_VERSION = 1
DEFAULT_STANDARDIZATION_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_standardization"
DEFAULT_STANDARDIZATION_REPORT_DIR = PROJECT_ROOT / "reports" / "research_standardization"
METHOD_ORDER = (
    "raw",
    "mad_winsorized",
    "industry_neutralized",
    "industry_marketcap_neutralized",
)
REAL_STATUSES = {
    RevisionStatus.OBSERVED.value,
    RevisionStatus.BACKFILLED_RELIABLE.value,
}


class ResearchStandardizationError(ValueError):
    """Raised when the Stage 0.5 read-only diagnostic cannot be produced."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchStandardizationError(f"{field} must be an object")
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _bare_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return code.split(".", 1)[0] if "." in code else code


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


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchStandardizationError(f"cannot load {field}: {exc}") from exc


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _stdev(values: Sequence[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _median(values: Sequence[float]) -> float:
    return statistics.median(values)


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


def _rank_ic(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = _mean(left_ranks)
    right_mean = _mean(right_ranks)
    if left_mean is None or right_mean is None:
        return None
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_ranks, right_ranks)
    )
    left_var = sum((value - left_mean) ** 2 for value in left_ranks)
    right_var = sum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_var * right_var)
    return numerator / denominator if denominator > 0 else None


def _winsorize(values: Sequence[float], mad_multiplier: float) -> list[float]:
    center = _median(values)
    mad = _median([abs(value - center) for value in values])
    if mad <= 0:
        return list(values)
    scale = 1.4826 * mad
    low = center - mad_multiplier * scale
    high = center + mad_multiplier * scale
    return [min(high, max(low, value)) for value in values]


def _group_center(values: Sequence[float], groups: Sequence[str]) -> list[float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for value, group in zip(values, groups):
        grouped[group].append(value)
    means = {group: float(statistics.fmean(items)) for group, items in grouped.items()}
    return [value - means[group] for value, group in zip(values, groups)]


def _industry_marketcap_residual(
    values: Sequence[float],
    industries: Sequence[str],
    market_caps: Sequence[float],
) -> list[float]:
    log_caps = [math.log(value) if value > 0 else 0.0 for value in market_caps]
    factor_residual = _group_center(values, industries)
    cap_residual = _group_center(log_caps, industries)
    denominator = sum(value * value for value in cap_residual)
    if denominator <= 1e-18:
        return factor_residual
    beta = sum(
        cap_value * factor_value
        for cap_value, factor_value in zip(cap_residual, factor_residual)
    ) / denominator
    return [
        factor_value - beta * cap_value
        for factor_value, cap_value in zip(factor_residual, cap_residual)
    ]


def load_market_cap_by_date(
    database: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, dict[str, float]]:
    """Load current total/circulating market cap values without writing to the DB."""
    target = validate_database_path(database)
    result: dict[str, dict[str, float]] = defaultdict(dict)
    with sqlite3.connect(str(target)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        if "current_daily_basic_metrics" not in tables:
            return {}
        clauses = [
            "effective_status IN ('observed','backfilled_reliable')"
        ]
        params: list[Any] = []
        if start_date:
            clauses.append("json_extract(record_json, '$.trade_date') >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("json_extract(record_json, '$.trade_date') <= ?")
            params.append(end_date)
        sql = "SELECT record_json FROM current_daily_basic_metrics"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        for (record_json,) in connection.execute(sql, tuple(params)):
            try:
                record = json.loads(record_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(record, Mapping):
                continue
            trade_date = str(record.get("trade_date") or "")
            code = str(record.get("ts_code") or "")
            if not trade_date or not code:
                continue
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            market_cap = _number(record.get("total_mv")) or _number(record.get("circ_mv"))
            if market_cap is None or market_cap <= 0:
                continue
            result[trade_date][code] = market_cap
    return {date: dict(values) for date, values in result.items()}


def load_pit_industry_by_date(
    database: str | Path,
    *,
    decision_dates: Sequence[str],
    codes: Sequence[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Resolve active industry membership for each requested decision date."""
    target = validate_database_path(database)
    selected_codes = {_bare_code(code) for code in codes or () if _bare_code(code)}
    timelines: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in RevisionStore(target).current_records("sector_membership"):
        if str(row.get("effective_status") or row.get("status") or "") not in REAL_STATUSES:
            continue
        record = row.get("record") if isinstance(row.get("record"), Mapping) else row
        code = _bare_code(record.get("con_code") or record.get("ts_code"))
        industry_name = str(record.get("industry_name") or "").strip()
        in_date = str(record.get("in_date") or "")
        out_date = str(record.get("out_date") or "")
        if not code or not industry_name or not in_date:
            continue
        if selected_codes and code not in selected_codes:
            continue
        timelines[code].append(
            {
                "industry_name": industry_name,
                "in_date": in_date,
                "out_date": out_date,
                "index_code": str(record.get("index_code") or ""),
            }
        )

    ordered: dict[str, dict[str, Any]] = {}
    for code, entries in timelines.items():
        ordered_entries = sorted(
            entries,
            key=lambda item: (
                item["in_date"],
                item["index_code"],
                item["industry_name"],
            ),
        )
        ordered[code] = {
            "entries": ordered_entries,
            "in_dates": [item["in_date"] for item in ordered_entries],
        }

    result: dict[str, dict[str, str]] = {}
    for decision_date in sorted({str(value) for value in decision_dates if value}):
        active_by_code: dict[str, str] = {}
        for code, timeline in ordered.items():
            entries = timeline["entries"]
            in_dates = timeline["in_dates"]
            position = bisect_right(in_dates, decision_date) - 1
            if position < 0:
                continue
            active = [
                item
                for item in entries[: position + 1]
                if not item["out_date"] or item["out_date"] > decision_date
            ]
            if not active:
                continue
            industries = sorted({item["industry_name"] for item in active})
            if industries:
                active_by_code[code] = industries[0]
        result[decision_date] = active_by_code
    return result


def _observations(
    rows: Sequence[Mapping[str, Any]],
    factor: str,
    *,
    industry_by_code: Mapping[str, str],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for row in rows:
        if row.get("replay_ready") is not True:
            continue
        raw_inputs = row.get("raw_inputs")
        labels = row.get("labels")
        if not isinstance(raw_inputs, Mapping) or not isinstance(labels, Mapping):
            continue
        value = _number(raw_inputs.get(factor))
        forward_return = _number(labels.get("forward_return"))
        if value is None or forward_return is None:
            continue
        code = str(row.get("code") or "")
        industry = str(industry_by_code.get(code) or "").strip() or None
        observations.append(
            {
                "code": code,
                "value": value,
                "forward_return": forward_return,
                "industry": industry,
            }
        )
    return observations


def _data_coverage(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    industry_by_date: Mapping[str, Mapping[str, str]] | None,
    industry_by_code: Mapping[str, str],
    market_cap_by_date: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    total = 0
    industry_covered = 0
    market_cap_covered = 0
    for snapshot in snapshots:
        decision_date = str(snapshot.get("decision_date") or "")
        industries = (
            industry_by_date.get(decision_date, {})
            if industry_by_date is not None
            else industry_by_code
        )
        market_caps = market_cap_by_date.get(decision_date, {})
        for row in snapshot.get("rows") or []:
            if not isinstance(row, Mapping) or row.get("replay_ready") is not True:
                continue
            code = _bare_code(row.get("code"))
            if not code:
                continue
            total += 1
            if str(industries.get(code) or "").strip():
                industry_covered += 1
            if _number(market_caps.get(code)) is not None:
                market_cap_covered += 1
    return {
        "replay_ready_rows": total,
        "industry_covered_rows": industry_covered,
        "industry_coverage_ratio": round(industry_covered / total, 8) if total else 0.0,
        "market_cap_covered_rows": market_cap_covered,
        "market_cap_coverage_ratio": round(market_cap_covered / total, 8) if total else 0.0,
    }


def _method_values(
    observations: Sequence[Mapping[str, Any]],
    *,
    method: str,
    mad_multiplier: float,
    market_caps: Mapping[str, float] | None,
    min_marketcap_coverage: float,
) -> tuple[list[float], list[float]] | None:
    selected = list(observations)
    if method in {"industry_neutralized", "industry_marketcap_neutralized"}:
        selected = [
            observation
            for observation in selected
            if str(observation.get("industry") or "").strip()
        ]
        industry_observation_count = len(selected)
        if industry_observation_count < 3:
            return None
    if method == "industry_marketcap_neutralized":
        if not market_caps:
            return None
        selected = [
            observation
            for observation in selected
            if _number(market_caps.get(str(observation["code"]))) is not None
        ]
        coverage = (
            len(selected) / industry_observation_count
            if industry_observation_count
            else 0.0
        )
        if coverage < min_marketcap_coverage:
            return None
    values = [float(observation["value"]) for observation in selected]
    returns = [float(observation["forward_return"]) for observation in selected]
    if method == "raw":
        return values, returns
    winsorized = _winsorize(values, mad_multiplier)
    if method == "mad_winsorized":
        return winsorized, returns
    industries = [str(observation["industry"]) for observation in selected]
    if method == "industry_neutralized":
        return _group_center(winsorized, industries), returns
    if method == "industry_marketcap_neutralized":
        market_values = [
            float(market_caps[str(observation["code"])]) for observation in selected
        ]
        return _industry_marketcap_residual(winsorized, industries, market_values), returns
    raise ResearchStandardizationError(f"unknown standardization method: {method}")


def _sign(value: float | None, epsilon: float) -> int:
    if value is None or abs(value) <= epsilon:
        return 0
    return 1 if value > 0 else -1


def _summarize_method(daily: Mapping[str, float], *, epsilon: float) -> dict[str, Any]:
    values = [float(value) for value in daily.values()]
    mean_value = _mean(values)
    stdev_value = _stdev(values)
    negative_count = sum(value < -epsilon for value in values)
    positive_count = sum(value > epsilon for value in values)
    zero_count = len(values) - negative_count - positive_count
    mean_sign = _sign(mean_value, epsilon)
    sign_matches = sum(_sign(value, epsilon) == mean_sign for value in values)
    return {
        "available_dates": len(values),
        "mean_ic": round(mean_value, 8) if mean_value is not None else None,
        "median_ic": round(_median(values), 8) if values else None,
        "stdev_ic": round(stdev_value, 8) if stdev_value is not None else None,
        "icir": round(mean_value / stdev_value, 8)
        if mean_value is not None and stdev_value and stdev_value > 0
        else None,
        "t_stat": round(mean_value / stdev_value * math.sqrt(len(values)), 8)
        if mean_value is not None and stdev_value and stdev_value > 0
        else None,
        "negative_rate": round(negative_count / len(values), 8) if values else 0.0,
        "positive_rate": round(positive_count / len(values), 8) if values else 0.0,
        "zero_rate": round(zero_count / len(values), 8) if values else 0.0,
        "mean_sign": mean_sign,
        "sign_consistency": round(sign_matches / len(values), 8) if values else 0.0,
        "daily_ic": {date: round(value, 8) for date, value in sorted(daily.items())},
    }


def _common_date_sign_consistency(
    method_daily: Mapping[str, Mapping[str, float]],
    *,
    epsilon: float,
) -> tuple[float, dict[str, int], int]:
    available = {
        method: daily for method, daily in method_daily.items() if daily
    }
    if not available:
        return 0.0, {}, 0
    common_dates = sorted(set.intersection(*(set(daily) for daily in available.values())))
    counts = {method: 0 for method in available}
    consistent = 0
    for date in common_dates:
        signs = {
            method: _sign(float(daily[date]), epsilon)
            for method, daily in available.items()
        }
        nonzero = {value for value in signs.values() if value != 0}
        if len(nonzero) == 1:
            consistent += 1
            for method, value in signs.items():
                counts[method] += int(value != 0)
    return (
        round(consistent / len(common_dates), 8) if common_dates else 0.0,
        counts,
        len(common_dates),
    )


def _flip_rate_vs_raw(
    raw_daily: Mapping[str, float],
    method_daily: Mapping[str, float],
    *,
    epsilon: float,
) -> float | None:
    common_dates = sorted(set(raw_daily) & set(method_daily))
    if not common_dates:
        return None
    flips = sum(
        _sign(raw_daily[date], epsilon) != _sign(method_daily[date], epsilon)
        and _sign(raw_daily[date], epsilon) != 0
        and _sign(method_daily[date], epsilon) != 0
        for date in common_dates
    )
    return round(flips / len(common_dates), 8)


def _classify_factor(
    summaries: Mapping[str, Mapping[str, Any]],
    *,
    min_dates: int,
    epsilon: float,
) -> tuple[str, str]:
    available = {
        method: summary
        for method, summary in summaries.items()
        if int(summary.get("available_dates") or 0) >= min_dates
        and summary.get("mean_ic") is not None
    }
    if not available:
        return "insufficient_data", "exclude_until_data_available"
    signs = {_sign(float(summary["mean_ic"]), epsilon) for summary in available.values()}
    signs.discard(0)
    if len(signs) == 1:
        sign = next(iter(signs))
        consistency = min(
            float(summary.get("sign_consistency") or 0.0)
            for summary in available.values()
        )
        if consistency >= 0.70:
            return (
                "stable_negative" if sign < 0 else "stable_positive",
                "retain_with_sign_control",
            )
        return "weak_but_same_sign", "retain_for_phase1_review"
    return "preprocessing_sensitive", "downweight_or_exclude_until_phase4"


def _evaluate_factor(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    factor: str,
    industry_by_date: Mapping[str, Mapping[str, str]] | None,
    industry_by_code: Mapping[str, str],
    market_cap_by_date: Mapping[str, Mapping[str, float]],
    mad_multiplier: float,
    min_rows_per_date: int,
    min_marketcap_coverage: float,
    min_dates: int,
    epsilon: float,
) -> dict[str, Any]:
    method_daily: dict[str, dict[str, float]] = {method: {} for method in METHOD_ORDER}
    method_rows: dict[str, dict[str, int]] = {method: {} for method in METHOD_ORDER}
    for snapshot in snapshots:
        decision_date = str(snapshot.get("decision_date") or "")
        rows = [
            row for row in snapshot.get("rows") or [] if isinstance(row, Mapping)
        ]
        resolved_industry = (
            industry_by_date.get(decision_date, {})
            if industry_by_date is not None
            else industry_by_code
        )
        observations = _observations(
            rows,
            factor,
            industry_by_code=resolved_industry,
        )
        if len(observations) < min_rows_per_date:
            continue
        market_caps = market_cap_by_date.get(decision_date) or {}
        for method in METHOD_ORDER:
            method_values = _method_values(
                observations,
                method=method,
                mad_multiplier=mad_multiplier,
                market_caps=market_caps,
                min_marketcap_coverage=min_marketcap_coverage,
            )
            if method_values is None:
                continue
            values, returns = method_values
            ic = _rank_ic(values, returns)
            if ic is None:
                continue
            method_daily[method][decision_date] = ic
            method_rows[method][decision_date] = len(values)
    summaries = {
        method: _summarize_method(method_daily[method], epsilon=epsilon)
        for method in METHOD_ORDER
    }
    for method in METHOD_ORDER:
        summaries[method]["rows_by_date"] = {
            date: count for date, count in sorted(method_rows[method].items())
        }
    all_method_consistency, consistent_counts, common_dates = (
        _common_date_sign_consistency(method_daily, epsilon=epsilon)
    )
    raw_daily = method_daily["raw"]
    flip_rates = {
        method: _flip_rate_vs_raw(raw_daily, method_daily[method], epsilon=epsilon)
        for method in METHOD_ORDER
        if method != "raw"
    }
    classification, recommendation = _classify_factor(
        summaries,
        min_dates=min_dates,
        epsilon=epsilon,
    )
    return {
        "factor": factor,
        "methods": summaries,
        "common_dates_across_methods": common_dates,
        "all_method_sign_consistency": all_method_consistency,
        "consistent_sign_dates_by_method": consistent_counts,
        "sign_flip_rate_vs_raw": flip_rates,
        "classification": classification,
        "recommendation": recommendation,
    }


def build_standardization_diagnostic(
    *,
    snapshots: Sequence[Mapping[str, Any]],
    industry_by_date: Mapping[str, Mapping[str, str]] | None = None,
    industry_by_code: Mapping[str, str] | None = None,
    market_cap_by_date: Mapping[str, Mapping[str, float]],
    industry_source: str,
    industry_lookahead_safe: bool,
    market_cap_source: str,
    mad_multiplier: float = 3.0,
    min_rows_per_date: int = 5,
    min_marketcap_coverage: float = 0.80,
    min_dates: int = 20,
    sign_epsilon: float = 1e-8,
) -> dict[str, Any]:
    """Build a read-only four-method factor IC comparison."""
    if mad_multiplier <= 0:
        raise ResearchStandardizationError("mad_multiplier must be positive")
    if min_rows_per_date < 3:
        raise ResearchStandardizationError("min_rows_per_date must be at least 3")
    if not 0 <= min_marketcap_coverage <= 1:
        raise ResearchStandardizationError(
            "min_marketcap_coverage must be in [0, 1]"
        )
    if min_dates <= 0:
        raise ResearchStandardizationError("min_dates must be positive")
    factor_results = [
        _evaluate_factor(
            snapshots,
            factor=factor,
            industry_by_date=industry_by_date,
            industry_by_code=industry_by_code or {},
            market_cap_by_date=market_cap_by_date,
            mad_multiplier=mad_multiplier,
            min_rows_per_date=min_rows_per_date,
            min_marketcap_coverage=min_marketcap_coverage,
            min_dates=min_dates,
            epsilon=sign_epsilon,
        )
        for factor in FEATURE_NAMES
    ]
    data_coverage = _data_coverage(
        snapshots,
        industry_by_date=industry_by_date,
        industry_by_code=industry_by_code or {},
        market_cap_by_date=market_cap_by_date,
    )
    method_availability = {
        method: sum(
            int(result["methods"][method]["available_dates"]) >= min_dates
            for result in factor_results
        )
        for method in METHOD_ORDER
    }
    classifications: dict[str, list[str]] = defaultdict(list)
    for result in factor_results:
        classifications[str(result["classification"])].append(str(result["factor"]))
    negative_mean_factors: list[str] = []
    positive_mean_factors: list[str] = []
    for result in factor_results:
        signs = {
            _sign(float(summary["mean_ic"]), sign_epsilon)
            for summary in result["methods"].values()
            if summary.get("mean_ic") is not None
        }
        signs.discard(0)
        if signs == {-1}:
            negative_mean_factors.append(str(result["factor"]))
        elif signs == {1}:
            positive_mean_factors.append(str(result["factor"]))
    findings: list[str] = []
    stable_negative = classifications.get("stable_negative", [])
    sensitive = classifications.get("preprocessing_sensitive", [])
    insufficient = classifications.get("insufficient_data", [])
    findings.append(
        "stable negative after all available preprocessing: "
        + (", ".join(stable_negative) if stable_negative else "none")
    )
    findings.append(
        "sign-sensitive to preprocessing: "
        + (", ".join(sensitive) if sensitive else "none")
    )
    findings.append(
        "negative mean IC across all available methods: "
        + (", ".join(sorted(negative_mean_factors)) if negative_mean_factors else "none")
    )
    findings.append(
        "positive mean IC across all available methods: "
        + (", ".join(sorted(positive_mean_factors)) if positive_mean_factors else "none")
    )
    if insufficient:
        findings.append("insufficient data: " + ", ".join(insufficient))
    if method_availability["industry_marketcap_neutralized"] == 0:
        findings.append(
            "industry + market-cap IC unavailable because industry or market-cap coverage is insufficient"
        )
    industry_mapping_hash = sha256_bytes(
        canonical_json_bytes(
            {
                "industry_by_date": industry_by_date,
                "industry_by_code": dict(industry_by_code or {}),
            },
            trailing_newline=False,
        )
    )
    diagnosis_identity = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_ids": [str(item.get("snapshot_id") or "") for item in snapshots],
        "industry_source": str(industry_source),
        "industry_lookahead_safe": bool(industry_lookahead_safe),
        "industry_mapping_hash": industry_mapping_hash,
        "market_cap_source": str(market_cap_source),
        "mad_multiplier": float(mad_multiplier),
        "min_rows_per_date": int(min_rows_per_date),
        "min_marketcap_coverage": float(min_marketcap_coverage),
        "min_dates": int(min_dates),
        "sign_epsilon": float(sign_epsilon),
    }
    diagnosis_id = sha256_bytes(
        canonical_json_bytes(diagnosis_identity, trailing_newline=False)
    )
    date_values = sorted(
        str(item.get("decision_date") or "")
        for item in snapshots
        if item.get("decision_date")
    )
    status = (
        "completed"
        if method_availability["industry_marketcap_neutralized"] > 0
        else "partial_marketcap_unavailable"
    )
    return {
        "artifact_type": "kinace_v3_factor_standardization_diagnostic",
        "schema_version": SCHEMA_VERSION,
        "diagnosis_id": diagnosis_id,
        "status": status,
        "snapshot_count": len(snapshots),
        "date_range": {
            "start": date_values[0] if date_values else None,
            "end": date_values[-1] if date_values else None,
        },
        "settings": {
            "mad_multiplier": float(mad_multiplier),
            "min_rows_per_date": int(min_rows_per_date),
            "min_marketcap_coverage": float(min_marketcap_coverage),
            "min_dates": int(min_dates),
            "sign_epsilon": float(sign_epsilon),
        },
        "data_sources": {
            "industry_source": str(industry_source),
            "industry_lookahead_safe": bool(industry_lookahead_safe),
            "industry_mapping_hash": industry_mapping_hash,
            "market_cap_source": str(market_cap_source),
        },
        "data_coverage": data_coverage,
        "methods": list(METHOD_ORDER),
        "method_availability": method_availability,
        "factors": factor_results,
        "classifications": {
            key: sorted(value) for key, value in sorted(classifications.items())
        },
        "negative_mean_ic_factors": sorted(negative_mean_factors),
        "positive_mean_ic_factors": sorted(positive_mean_factors),
        "findings": findings,
        "read_only": True,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }


def load_snapshot_inputs(
    snapshot_dir: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in discover_snapshots(
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    ):
        payload = load_feature_snapshot_artifact(str(item["path"]))
        result.append(
            {
                "decision_date": str(item["decision_date"]),
                "snapshot_id": str(item["snapshot_id"]),
                "artifact_path": str(item["path"]),
                "rows": list(payload.get("rows") or []),
            }
        )
    return result


def _markdown_report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Kinace Stage 0.5 Factor Standardization Diagnostic",
        "",
        f"- Diagnosis ID: `{payload['diagnosis_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Dates: `{payload['date_range']['start']}` to `{payload['date_range']['end']}`",
        f"- Industry source: `{payload['data_sources']['industry_source']}`",
        f"- Industry lookahead safe: `{payload['data_sources']['industry_lookahead_safe']}`",
        f"- Market-cap source: `{payload['data_sources']['market_cap_source']}`",
        "",
        "| Factor | Raw | MAD Winsorized | Industry | Industry + Market Cap | Sign Consistency | Classification |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for result in payload["factors"]:
        methods = result["methods"]
        lines.append(
            f"| `{result['factor']}` | {methods['raw']['mean_ic']} | "
            f"{methods['mad_winsorized']['mean_ic']} | "
            f"{methods['industry_neutralized']['mean_ic']} | "
            f"{methods['industry_marketcap_neutralized']['mean_ic']} | "
            f"{result['all_method_sign_consistency']} | `{result['classification']}` |"
        )
    lines.extend(["", "## Findings", ""])
    lines.extend(f"- {finding}" for finding in payload["findings"])
    lines.append("")
    return "\n".join(lines)


def run_standardization_diagnostic(
    database: str | Path | None,
    *,
    snapshot_dir: str | Path,
    start_date: str | None = None,
    end_date: str | None = None,
    industry_by_date: Mapping[str, Mapping[str, str]] | None = None,
    industry_by_code: Mapping[str, str] | None = None,
    industry_source: str | None = None,
    industry_lookahead_safe: bool | None = None,
    market_cap_by_date: Mapping[str, Mapping[str, float]] | None = None,
    market_cap_source: str = "database_current_total_mv",
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_STANDARDIZATION_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_STANDARDIZATION_REPORT_DIR,
    mad_multiplier: float = 3.0,
    min_rows_per_date: int = 5,
    min_marketcap_coverage: float = 0.80,
    min_dates: int = 20,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    snapshots = load_snapshot_inputs(
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    if not snapshots:
        raise ResearchStandardizationError("no snapshots were selected")
    resolved_industry_by_date: dict[str, dict[str, str]] = {}
    if industry_by_date is not None:
        resolved_industry_by_date = {
            str(date): {_bare_code(code): str(value) for code, value in values.items()}
            for date, values in industry_by_date.items()
        }
        resolved_industry: dict[str, str] = {}
        resolved_source = industry_source or "pit_membership"
        resolved_lookahead_safe = (
            True if industry_lookahead_safe is None else bool(industry_lookahead_safe)
        )
    elif industry_by_code is not None:
        resolved_industry = {
            _bare_code(code): str(value) for code, value in industry_by_code.items()
        }
        resolved_source = industry_source or "current_proxy"
        resolved_lookahead_safe = bool(industry_lookahead_safe)
    else:
        decision_dates = [
            str(snapshot.get("decision_date") or "")
            for snapshot in snapshots
            if snapshot.get("decision_date")
        ]
        codes = sorted(
            {
                _bare_code(row.get("code"))
                for snapshot in snapshots
                for row in snapshot.get("rows") or []
                if isinstance(row, Mapping) and _bare_code(row.get("code"))
            }
        )
        resolved_industry_by_date = load_pit_industry_by_date(
            target,
            decision_dates=decision_dates,
            codes=codes,
        )
        has_pit_industry = any(resolved_industry_by_date.values())
        if has_pit_industry:
            resolved_industry = {}
            resolved_source = industry_source or "pit_membership"
            resolved_lookahead_safe = (
                True if industry_lookahead_safe is None else bool(industry_lookahead_safe)
            )
        else:
            resolved_industry = load_industry_map(target)
            resolved_source = industry_source or "current_proxy"
            resolved_lookahead_safe = bool(industry_lookahead_safe)
    resolved_market_cap = (
        {
            str(date): {str(code): float(value) for code, value in values.items()}
            for date, values in market_cap_by_date.items()
        }
        if market_cap_by_date is not None
        else load_market_cap_by_date(
            target,
            start_date=start_date,
            end_date=end_date,
        )
    )
    if not resolved_market_cap and market_cap_by_date is None:
        market_cap_source = "unavailable"
    payload = build_standardization_diagnostic(
        snapshots=snapshots,
        industry_by_date=resolved_industry_by_date,
        industry_by_code=resolved_industry,
        market_cap_by_date=resolved_market_cap,
        industry_source=resolved_source,
        industry_lookahead_safe=resolved_lookahead_safe,
        market_cap_source=market_cap_source,
        mad_multiplier=mad_multiplier,
        min_rows_per_date=min_rows_per_date,
        min_marketcap_coverage=min_marketcap_coverage,
        min_dates=min_dates,
    )
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(output_dir).resolve(strict=False)
        / f"standardization-{payload['diagnosis_id']}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else Path(report_dir).resolve(strict=False)
        / f"standardization-{payload['diagnosis_id']}.md"
    )
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    return payload


__all__ = [
    "DEFAULT_STANDARDIZATION_OUTPUT_DIR",
    "DEFAULT_STANDARDIZATION_REPORT_DIR",
    "METHOD_ORDER",
    "ResearchStandardizationError",
    "SCHEMA_VERSION",
    "build_standardization_diagnostic",
    "load_market_cap_by_date",
    "load_pit_industry_by_date",
    "load_snapshot_inputs",
    "run_standardization_diagnostic",
]
