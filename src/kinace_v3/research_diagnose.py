"""Trade-level attribution for a rejected or selected research holdout."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .data_collection.models import RevisionStatus
from .data_collection.store import RevisionStore
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .optimization import score_portfolio_objective
from .portfolio_backtest import load_market_bars, run_portfolio_backtest
from .score_replay import validate_candidate_config
from .trial_backtest import (
    RETURN_ANOMALY_POLICY,
    initialize_trial_backtest_schema,
    normalize_backtest_config,
)


SCHEMA_VERSION = 1
DEFAULT_DIAGNOSIS_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "diagnosis"
DEFAULT_DIAGNOSIS_REPORT_DIR = PROJECT_ROOT / "reports" / "diagnosis"
DEFAULT_BASELINE_CANDIDATE = PROJECT_ROOT / "reports" / "baseline_v32_candidate.json"
DEFAULT_BASELINE_CONFIG = PROJECT_ROOT / "reports" / "baseline_v32_backtest_config.json"
SAME_WINDOW_METRIC_KEYS = (
    "total_return",
    "annualized_return",
    "excess_return",
    "max_drawdown",
    "profit_factor",
    "win_rate",
    "turnover",
    "trade_count",
    "total_fees",
    "total_slippage",
)
COST_CONFIG_KEYS = (
    "commission_rate",
    "min_commission",
    "stamp_duty_rate",
    "transfer_fee_rate",
    "slippage_bps",
)
TRADE_KEY_FIELDS = ("code", "decision_date", "entry_date", "exit_date")


class ResearchDiagnosisError(ValueError):
    """Raised when holdout attribution cannot be produced."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDiagnosisError(f"{field} must be an object")
    return value


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchDiagnosisError(f"cannot load {field}: {exc}") from exc


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


def initialize_diagnosis_schema(database: str | Path | None = None) -> Path:
    target = initialize_trial_backtest_schema(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_diagnoses (
                diagnosis_id TEXT PRIMARY KEY,
                loop_id TEXT,
                candidate_signature TEXT,
                holdout_start TEXT NOT NULL,
                holdout_end TEXT NOT NULL,
                candidate_run_id TEXT NOT NULL,
                baseline_run_id TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                summary_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_research_diagnoses_holdout
                ON research_diagnoses(holdout_start, holdout_end);
            """
        )
    return target


def load_industry_map(database: str | Path) -> dict[str, str]:
    store = RevisionStore(database)
    result: dict[str, str] = {}
    for row in store.current_records("stock_metadata"):
        status = str(row.get("effective_status") or row.get("status") or "")
        if status not in {
            RevisionStatus.OBSERVED.value,
            RevisionStatus.BACKFILLED_RELIABLE.value,
        }:
            continue
        record = row.get("record") if isinstance(row.get("record"), Mapping) else row
        code = str(record.get("ts_code") or "")
        if code:
            result[code] = str(record.get("industry") or "unknown")
    return result


def build_market_proxy(
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for code, code_bars in bars.items():
        for bar in code_bars:
            trade_date = str(bar.get("trade_date") or "")
            if start_date <= trade_date <= end_date:
                by_date[trade_date][str(code)] = bar
    rows: list[dict[str, Any]] = []
    previous: dict[str, float] = {}
    rolling_returns: list[float] = []
    for trade_date in sorted(by_date):
        current = {
            code: float(bar["close"])
            for code, bar in by_date[trade_date].items()
        }
        observations = [
            (code, current[code] / previous[code] - 1.0)
            for code in sorted(set(previous) & set(current))
            if previous[code] > 0
        ]
        benchmark_return = (
            statistics.fmean(value for _, value in observations)
            if observations
            else 0.0
        )
        anomaly_rule = RETURN_ANOMALY_POLICY["sanitized_auxiliary"]
        anomaly_threshold = float(anomaly_rule["threshold"])
        exclusion_reason = str(anomaly_rule["exclusion_reason"])
        sanitized_returns: list[float] = []
        exclusions: list[dict[str, Any]] = []
        for code, raw_return in observations:
            if abs(raw_return) > anomaly_threshold:
                exclusions.append(
                    {
                        "date": trade_date,
                        "code": code,
                        "raw_return": round(raw_return, 12),
                        "exclusion_reason": exclusion_reason,
                    }
                )
            else:
                sanitized_returns.append(raw_return)
        sanitized_return = (
            statistics.fmean(sanitized_returns) if sanitized_returns else 0.0
        )
        rolling_returns.append(benchmark_return)
        volatility_window = rolling_returns[-5:]
        rolling_volatility = (
            statistics.stdev(volatility_window)
            if len(volatility_window) >= 2
            else 0.0
        )
        day_bars = by_date[trade_date]
        rows.append(
            {
                "trade_date": trade_date,
                "benchmark_return": benchmark_return,
                "benchmark_sanitized_return": sanitized_return,
                "benchmark_exclusions": exclusions,
                "benchmark_exclusion_count": len(exclusions),
                "rolling_volatility_5d": rolling_volatility,
                "limit_up_count": sum(
                    bool(bar.get("is_limit_up", False)) for bar in day_bars.values()
                ),
                "limit_down_count": sum(
                    bool(bar.get("is_limit_down", False)) for bar in day_bars.values()
                ),
                "suspended_count": sum(
                    bool(bar.get("is_suspended", False)) for bar in day_bars.values()
                ),
                "eligible_count": len(day_bars),
            }
        )
        previous = current
    return rows


def _daily_rows(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    market_proxy: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidate_equity = {
        str(point["trade_date"]): point for point in candidate.get("equity") or []
    }
    baseline_equity = {
        str(point["trade_date"]): point for point in baseline.get("equity") or []
    }
    proxy = {str(row["trade_date"]): row for row in market_proxy}
    dates = sorted(candidate_equity)
    rows: list[dict[str, Any]] = []
    previous_candidate: float | None = None
    previous_baseline: float | None = None
    peak = None
    volatility_values = [
        float(proxy[date].get("rolling_volatility_5d") or 0.0)
        for date in dates
        if date in proxy
    ]
    high_vol_threshold = (
        statistics.median(volatility_values) if volatility_values else 0.0
    )
    for trade_date in dates:
        candidate_point = candidate_equity[trade_date]
        baseline_point = baseline_equity.get(trade_date, {})
        candidate_total = float(candidate_point.get("total_assets") or 0.0)
        baseline_total = float(baseline_point.get("total_assets") or candidate_total)
        candidate_return = (
            candidate_total / previous_candidate - 1.0
            if previous_candidate and previous_candidate > 0
            else 0.0
        )
        baseline_return = (
            baseline_total / previous_baseline - 1.0
            if previous_baseline and previous_baseline > 0
            else 0.0
        )
        peak = candidate_total if peak is None else max(peak, candidate_total)
        proxy_row = proxy.get(trade_date, {})
        rolling_volatility = float(proxy_row.get("rolling_volatility_5d") or 0.0)
        volatility_state = (
            "high" if rolling_volatility > high_vol_threshold else "low"
        )
        limit_state = "balanced"
        if int(proxy_row.get("limit_down_count") or 0) > int(
            proxy_row.get("limit_up_count") or 0
        ):
            limit_state = "limit_down_dominant"
        elif int(proxy_row.get("limit_up_count") or 0) > int(
            proxy_row.get("limit_down_count") or 0
        ):
            limit_state = "limit_up_dominant"
        rows.append(
            {
                "trade_date": trade_date,
                "candidate_total_assets": candidate_total,
                "candidate_pnl": candidate_total - previous_candidate
                if previous_candidate is not None
                else 0.0,
                "candidate_return": candidate_return,
                "candidate_drawdown": (peak - candidate_total) / peak
                if peak and peak > 0
                else 0.0,
                "baseline_total_assets": baseline_total,
                "baseline_return": baseline_return,
                "return_vs_baseline": candidate_return - baseline_return,
                "benchmark_return": float(
                    proxy_row.get("benchmark_return") or 0.0
                ),
                "excess_vs_benchmark": candidate_return
                - float(proxy_row.get("benchmark_return") or 0.0),
                "rolling_volatility_5d": rolling_volatility,
                "volatility_state": volatility_state,
                "limit_state": limit_state,
                "limit_up_count": int(proxy_row.get("limit_up_count") or 0),
                "limit_down_count": int(proxy_row.get("limit_down_count") or 0),
                "suspended_count": int(proxy_row.get("suspended_count") or 0),
                "positions": int(candidate_point.get("positions") or 0),
            }
        )
        previous_candidate = candidate_total
        previous_baseline = baseline_total
    return rows


def _group_trades(
    trades: Sequence[Mapping[str, Any]],
    key_function: Any,
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(key_function(trade))].append(trade)
    result = []
    for key, values in groups.items():
        pnl = sum(float(item.get("pnl") or 0.0) for item in values)
        wins = sum(float(item.get("pnl") or 0.0) > 0 for item in values)
        result.append(
            {
                "key": key,
                "trade_count": len(values),
                "pnl": round(pnl, 6),
                "win_rate": round(wins / len(values), 6) if values else 0.0,
                "average_return": round(
                    statistics.fmean(
                        float(item.get("return_pct") or 0.0) for item in values
                    ),
                    6,
                )
                if values
                else 0.0,
                "fees": round(
                    sum(float(item.get("fees") or 0.0) for item in values), 6
                ),
                "average_hold_sessions": round(
                    statistics.fmean(
                        float(item.get("hold_sessions") or 0.0) for item in values
                    ),
                    6,
                )
                if values
                else 0.0,
            }
        )
    return sorted(result, key=lambda item: (item["pnl"], item["key"]))


def _score_bucket(trade: Mapping[str, Any]) -> str:
    score = float((trade.get("metadata") or {}).get("score") or 0.0)
    if score < 0.55:
        return "00:<0.55"
    if score < 0.60:
        return "01:0.55-0.60"
    if score < 0.65:
        return "02:0.60-0.65"
    if score < 0.70:
        return "03:0.65-0.70"
    if score < 0.75:
        return "04:0.70-0.75"
    if score < 0.80:
        return "05:0.75-0.80"
    return "06:>=0.80"


def _state_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[f"benchmark_{'up' if float(row['benchmark_return']) >= 0 else 'down'}"].append(row)
        groups[f"volatility_{row['volatility_state']}"].append(row)
        groups[str(row["limit_state"])].append(row)
    result = []
    for key, values in groups.items():
        compounded = 1.0
        for item in values:
            compounded *= 1.0 + float(item["candidate_return"])
        result.append(
            {
                "state": key,
                "sessions": len(values),
                "candidate_return": round(compounded - 1.0, 6),
                "average_daily_return": round(
                    statistics.fmean(
                        float(item["candidate_return"]) for item in values
                    ),
                    6,
                )
                if values
                else 0.0,
                "average_benchmark_return": round(
                    statistics.fmean(
                        float(item["benchmark_return"]) for item in values
                    ),
                    6,
                )
                if values
                else 0.0,
            }
        )
    return sorted(result, key=lambda item: (item["candidate_return"], item["state"]))


def _metric_number(metrics: Mapping[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _trade_identity(trade: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(trade.get(field) or "") for field in TRADE_KEY_FIELDS)


def _same_window_comparison(
    *,
    candidate_report: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    objective: Mapping[str, Any] | None,
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
    same_window = candidate_range == baseline_range
    same_snapshot_set = bool(
        candidate_snapshot_set
        and baseline_snapshot_set
        and candidate_snapshot_set == baseline_snapshot_set
    )

    candidate_config = candidate_report.get("config")
    baseline_config = baseline_report.get("config")
    cost_fields_equal = None
    if isinstance(candidate_config, Mapping) and isinstance(baseline_config, Mapping):
        cost_fields_equal = all(
            candidate_config.get(key) == baseline_config.get(key)
            for key in COST_CONFIG_KEYS
        )

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

    deltas: dict[str, float] = {}
    for key in SAME_WINDOW_METRIC_KEYS:
        candidate_value = _metric_number(candidate_metrics, key)
        baseline_value = _metric_number(baseline_metrics, key)
        if candidate_value is not None and baseline_value is not None:
            deltas[key] = round(candidate_value - baseline_value, 8)

    candidate_objective = (
        score_portfolio_objective(candidate_metrics, objective)
        if objective
        else None
    )
    baseline_objective = (
        score_portfolio_objective(baseline_metrics, objective)
        if objective
        else None
    )
    candidate_gate_passed = bool(
        candidate_objective and candidate_objective.get("status") == "passed"
    )
    candidate_total_return = _metric_number(candidate_metrics, "total_return")
    baseline_total_return = _metric_number(baseline_metrics, "total_return")
    candidate_profit_factor = _metric_number(candidate_metrics, "profit_factor")
    baseline_profit_factor = _metric_number(baseline_metrics, "profit_factor")
    return_not_worse = bool(
        candidate_total_return is not None
        and baseline_total_return is not None
        and candidate_total_return >= baseline_total_return
    )
    profit_factor_not_worse = bool(
        candidate_profit_factor is not None
        and baseline_profit_factor is not None
        and candidate_profit_factor >= baseline_profit_factor
    )
    checks = {
        "same_window": same_window,
        "same_snapshot_set": same_snapshot_set,
        "cost_fields_equal": cost_fields_equal is True,
        "candidate_gate_passed": candidate_gate_passed,
        "return_not_worse": return_not_worse,
        "profit_factor_not_worse": profit_factor_not_worse,
    }
    return {
        "window": {
            "start": str(candidate_range.get("start") or ""),
            "end": str(candidate_range.get("end") or ""),
            "sessions": int(candidate_metrics.get("trading_days") or 0),
        },
        "same_window": same_window,
        "same_snapshot_set": same_snapshot_set,
        "snapshot_set_id_challenger": candidate_snapshot_set,
        "snapshot_set_id_baseline": baseline_snapshot_set,
        "cost_fields_equal": cost_fields_equal,
        "candidate_objective": candidate_objective,
        "baseline_objective": baseline_objective,
        "candidate_gate_passed": candidate_gate_passed,
        "candidate_hard_gate_failures": (
            list(candidate_objective.get("hard_gate_failures") or [])
            if candidate_objective
            else []
        ),
        "metric_delta_challenger_minus_baseline": deltas,
        "trade_overlap": {
            "baseline_trades": sum(baseline_trades.values()),
            "challenger_trades": sum(candidate_trades.values()),
            "common_trades": common_count,
            "union_trades": union_count,
            "jaccard": round(common_count / union_count, 8) if union_count else 1.0,
            "trade_key_fields": list(TRADE_KEY_FIELDS),
            "common_keys": ["|".join(key) for key in sorted(common.elements())],
        },
        "qualification_checks": checks,
        "verdict": (
            "challenger_noninferior"
            if all(checks.values())
            else "challenger_not_qualified"
        ),
    }


def build_diagnosis(
    *,
    candidate_report: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    industry_by_code: Mapping[str, str],
    candidate_signature: str | None,
    loop_id: str | None,
    code_version: str,
    objective: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_range = _mapping(candidate_report.get("date_range") or {}, "candidate.date_range")
    start_date = str(candidate_range.get("start") or "")
    end_date = str(candidate_range.get("end") or "")
    if not start_date or not end_date:
        raise ResearchDiagnosisError("candidate report has no date range")
    market_proxy = build_market_proxy(bars, start_date=start_date, end_date=end_date)
    market_proxy_exclusions = [
        exclusion
        for row in market_proxy
        for exclusion in row.get("benchmark_exclusions") or []
    ]
    daily = _daily_rows(candidate_report, baseline_report, market_proxy)
    trades = [
        dict(item) for item in candidate_report.get("trades") or [] if isinstance(item, Mapping)
    ]
    by_stock = _group_trades(trades, lambda item: item.get("code") or "unknown")
    by_industry = _group_trades(
        trades,
        lambda item: industry_by_code.get(str(item.get("code") or ""), "unknown"),
    )
    by_exit = _group_trades(trades, lambda item: item.get("exit_reason") or "unknown")
    by_score = _group_trades(trades, _score_bucket)
    losses = [item["pnl"] for item in by_stock if float(item["pnl"]) < 0]
    total_loss = abs(sum(losses))
    top3_loss = abs(sum(sorted(losses)[:3]))
    worst_days = sorted(daily, key=lambda item: float(item["candidate_pnl"]))[:10]
    best_days = sorted(daily, key=lambda item: float(item["candidate_pnl"]), reverse=True)[:10]
    candidate_metrics = dict(candidate_report.get("metrics") or {})
    baseline_metrics = dict(baseline_report.get("metrics") or {})
    same_window = _same_window_comparison(
        candidate_report=candidate_report,
        baseline_report=baseline_report,
        objective=objective,
    )
    findings: list[str] = []
    if total_loss > 0:
        findings.append(
            f"top 3 loss stocks account for {top3_loss / total_loss:.2%} of total gross losses"
        )
        if top3_loss / total_loss < 0.40:
            findings.append("losses are broad-based rather than concentrated in a few stocks")
    if by_stock:
        findings.append(f"worst stock: {by_stock[0]['key']} pnl={by_stock[0]['pnl']:.2f}")
    if by_industry:
        findings.append(
            f"worst industry: {by_industry[0]['key']} pnl={by_industry[0]['pnl']:.2f}"
        )
    if by_exit:
        findings.append(
            f"worst exit path: {by_exit[0]['key']} pnl={by_exit[0]['pnl']:.2f}"
        )
    if by_score:
        findings.append(
            f"worst score bucket: {by_score[0]['key']} pnl={by_score[0]['pnl']:.2f}"
        )
        highest_bucket = next(
            (item for item in by_score if str(item["key"]).startswith("06:")),
            None,
        )
        if highest_bucket is not None:
            findings.append(
                "highest score bucket performance: "
                f"{highest_bucket['key']} pnl={highest_bucket['pnl']:.2f} "
                f"win_rate={highest_bucket['win_rate']:.2%}"
            )
    stop_items = [
        item for item in by_exit if str(item["key"]).startswith("stop")
    ]
    stop_pnl = sum(float(item["pnl"]) for item in stop_items)
    if stop_pnl < 0:
        findings.append(
            f"stop/stop_gap exits account for pnl={stop_pnl:.2f} across "
            f"{sum(int(item['trade_count']) for item in stop_items)} trades"
        )
    state_by_name = {
        str(item["state"]): item for item in _state_summary(daily)
    }
    for state_name in (
        "benchmark_down",
        "volatility_high",
        "limit_down_dominant",
    ):
        state = state_by_name.get(state_name)
        if state is not None:
            findings.append(
                f"market state {state_name}: candidate_return="
                f"{state['candidate_return']:.4%} over {state['sessions']} sessions"
            )
    if worst_days:
        findings.append(
            f"worst day: {worst_days[0]['trade_date']} pnl="
            f"{worst_days[0]['candidate_pnl']:.2f} return="
            f"{worst_days[0]['candidate_return']:.4%}"
        )
    findings.append(
        "candidate return "
        f"{float(candidate_metrics.get('total_return') or 0.0):.4%} vs baseline "
        f"{float(baseline_metrics.get('total_return') or 0.0):.4%}"
    )
    diagnosis_id = sha256_bytes(
        canonical_json_bytes(
            {
                "candidate_run_id": candidate_report.get("run_id"),
                "baseline_run_id": baseline_report.get("run_id"),
                "candidate_signature": candidate_signature,
                "loop_id": loop_id,
                "start_date": start_date,
                "end_date": end_date,
                "code_version": code_version,
            },
            trailing_newline=False,
        )
    )
    return {
        "artifact_type": "kinace_v3_research_holdout_diagnosis",
        "schema_version": SCHEMA_VERSION,
        "diagnosis_id": diagnosis_id,
        "loop_id": loop_id,
        "candidate_signature": candidate_signature,
        "holdout": {"start": start_date, "end": end_date},
        "candidate_run_id": candidate_report.get("run_id"),
        "baseline_run_id": baseline_report.get("run_id"),
        "candidate_metrics": candidate_metrics,
        "baseline_metrics": baseline_metrics,
        "same_window_comparison": same_window,
        "daily": daily,
        "market_proxy": market_proxy,
        "market_proxy_label": "equal-weight market proxy",
        "market_proxy_is_official_index": False,
        "market_proxy_anomaly_policy": deepcopy(RETURN_ANOMALY_POLICY),
        "market_proxy_exclusions": market_proxy_exclusions,
        "by_stock": by_stock,
        "by_industry": by_industry,
        "by_exit_reason": by_exit,
        "by_score_bucket": by_score,
        "market_state_summary": _state_summary(daily),
        "worst_days": worst_days,
        "best_days": best_days,
        "findings": findings,
        "created_at_utc": _utc_now(),
    }


def _markdown_report(payload: Mapping[str, Any]) -> str:
    candidate = payload.get("candidate_metrics") or {}
    baseline = payload.get("baseline_metrics") or {}
    comparison = payload.get("same_window_comparison") or {}
    lines = [
        "# Kinace Research Holdout Diagnosis",
        "",
        f"- Diagnosis ID: `{payload['diagnosis_id']}`",
        f"- Holdout: `{payload['holdout']['start']}` to `{payload['holdout']['end']}`",
        f"- Candidate: `{payload.get('candidate_run_id')}`",
        f"- Baseline: `{payload.get('baseline_run_id')}`",
        f"- Benchmark: {payload.get('market_proxy_label', 'equal-weight market proxy')} "
        "(not an official index)",
        "",
        "## Summary",
        "",
        "| Metric | Candidate | Baseline |",
        "|---|---:|---:|",
        f"| Total return | {candidate.get('total_return')} | {baseline.get('total_return')} |",
        f"| Max drawdown | {candidate.get('max_drawdown')} | {baseline.get('max_drawdown')} |",
        f"| Profit factor | {candidate.get('profit_factor')} | {baseline.get('profit_factor')} |",
        f"| Win rate | {candidate.get('win_rate')} | {baseline.get('win_rate')} |",
        f"| Trades | {candidate.get('trade_count')} | {baseline.get('trade_count')} |",
        "",
        "## Same-Window Qualification",
        "",
        f"- Verdict: `{comparison.get('verdict')}`",
        f"- Same window: `{comparison.get('same_window')}`",
        f"- Same snapshot set: `{comparison.get('same_snapshot_set')}`",
        f"- Cost fields equal: `{comparison.get('cost_fields_equal')}`",
        f"- Candidate gate passed: `{comparison.get('candidate_gate_passed')}`",
        f"- Candidate hard-gate failures: `{comparison.get('candidate_hard_gate_failures')}`",
        "",
        "| Metric | Baseline | Challenger | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key in SAME_WINDOW_METRIC_KEYS:
        lines.append(
            f"| `{key}` | {baseline.get(key)} | {candidate.get(key)} | "
            f"{(comparison.get('metric_delta_challenger_minus_baseline') or {}).get(key)} |"
        )
    lines.extend(
        [
            "",
            "## Findings",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in payload.get("findings") or [])
    lines.extend(
        [
            "",
            "## Worst Days",
            "",
            "| Date | PnL | Return | Benchmark | Excess | Drawdown |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("worst_days") or []:
        lines.append(
            f"| `{row['trade_date']}` | {row['candidate_pnl']:.2f} | "
            f"{row['candidate_return']:.4%} | {row['benchmark_return']:.4%} | "
            f"{row['excess_vs_benchmark']:.4%} | {row['candidate_drawdown']:.4%} |"
        )
    for title, key in (
        ("Stock Contribution", "by_stock"),
        ("Industry Contribution", "by_industry"),
        ("Exit Reason", "by_exit_reason"),
        ("Score Bucket", "by_score_bucket"),
    ):
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Key | PnL | Trades | Win Rate | Average Return |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in (payload.get(key) or [])[:12]:
            lines.append(
                f"| `{row['key']}` | {row['pnl']:.2f} | {row['trade_count']} | "
                f"{row['win_rate']:.4%} | {row['average_return']:.4%} |"
            )
    lines.extend(
        [
            "",
            "## Market State",
            "",
            "| State | Sessions | Candidate Return | Average Benchmark |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in payload.get("market_state_summary") or []:
        lines.append(
            f"| `{row['state']}` | {row['sessions']} | {row['candidate_return']:.4%} | "
            f"{row['average_benchmark_return']:.4%} |"
        )
    exclusions = payload.get("market_proxy_exclusions") or []
    lines.extend(
        [
            "",
            "## Market Proxy Exclusions",
            "",
        ]
    )
    if exclusions:
        lines.extend(
            [
                "| Date | Code | Raw Return | Reason |",
                "|---|---|---:|---|",
            ]
        )
        for exclusion in exclusions:
            lines.append(
                f"| `{exclusion['date']}` | `{exclusion['code']}` | "
                f"{float(exclusion['raw_return']):.6%} | "
                f"`{exclusion['exclusion_reason']}` |"
            )
    else:
        lines.append("No sanitized auxiliary-curve exclusions.")
    lines.append("")
    return "\n".join(lines)


def run_research_diagnosis(
    database: str | Path | None,
    research_report_path: str | Path,
    *,
    baseline_candidate_path: str | Path = DEFAULT_BASELINE_CANDIDATE,
    baseline_config_path: str | Path = DEFAULT_BASELINE_CONFIG,
    snapshot_dir: str | Path,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_DIAGNOSIS_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_DIAGNOSIS_REPORT_DIR,
    code_version: str = "kinace-v3",
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    research = _mapping(
        _load_json(research_report_path, "research loop report"), "research loop report"
    )
    best_reference = research.get("best_challenger_path") or research.get(
        "best_candidate_path"
    )
    best = _mapping(
        _load_json(best_reference, "best challenger") if best_reference else research.get("best_candidate"),
        "best challenger",
    )
    candidate_model = validate_candidate_config(
        _mapping(best.get("model_config"), "best.model_config")
    )
    candidate_backtest = normalize_backtest_config(
        _mapping(best.get("backtest_config") or {}, "best.backtest_config")
    )
    holdout = _mapping(research.get("holdout") or {}, "research.holdout")
    holdout_range = holdout.get("date_range")
    if isinstance(holdout_range, Mapping):
        start_date = str(holdout_range.get("start") or "")
        end_date = str(holdout_range.get("end") or "")
    else:
        start_date = str(holdout.get("start") or "")
        end_date = str(holdout.get("end") or "")
    if not start_date or not end_date:
        raise ResearchDiagnosisError("research report has no holdout window")
    candidate_report_path = holdout.get("portfolio_output_path")
    if not candidate_report_path:
        raise ResearchDiagnosisError("research holdout has no candidate portfolio output")
    candidate_report = _mapping(
        _load_json(candidate_report_path, "candidate holdout report"),
        "candidate holdout report",
    )
    baseline_model = validate_candidate_config(
        _mapping(_load_json(baseline_candidate_path, "baseline candidate"), "baseline candidate")
    )
    baseline_config = normalize_backtest_config(
        _mapping(_load_json(baseline_config_path, "baseline config"), "baseline config")
    )
    bars = load_market_bars(target)
    diagnosis_id = sha256_bytes(
        canonical_json_bytes(
            {
                "loop_id": research.get("loop_id"),
                "candidate_signature": best.get("candidate_signature")
                or best.get("signature"),
                "candidate_run_id": candidate_report.get("run_id"),
                "baseline_candidate_id": baseline_model.get("version"),
                "start_date": start_date,
                "end_date": end_date,
                "code_version": code_version,
            },
            trailing_newline=False,
        )
    )
    diagnosis_dir = Path(output_dir).resolve(strict=False) / diagnosis_id
    diagnosis_dir.mkdir(parents=True, exist_ok=True)
    baseline_output = diagnosis_dir / "baseline-holdout.json"
    baseline_report = run_portfolio_backtest(
        target,
        baseline_model,
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
        bars=bars,
        config=baseline_config,
        output_path=baseline_output,
        code_version=code_version,
    )
    payload = build_diagnosis(
        candidate_report=candidate_report,
        baseline_report=baseline_report,
        bars=bars,
        industry_by_code=load_industry_map(target),
        candidate_signature=best.get("candidate_signature") or best.get("signature"),
        loop_id=research.get("loop_id"),
        code_version=code_version,
        objective=_mapping(research.get("objective") or {}, "research.objective"),
    )
    payload["challenger_model_config"] = holdout.get("model_config")
    payload["challenger_training"] = holdout.get("training")
    payload["diagnosis_id"] = diagnosis_id
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(report_dir).resolve(strict=False) / f"holdout-diagnosis-{diagnosis_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else artifact_path.with_suffix(".md")
    )
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    initialize_diagnosis_schema(target)
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO research_diagnoses (
                diagnosis_id, loop_id, candidate_signature, holdout_start,
                holdout_end, candidate_run_id, baseline_run_id, artifact_path,
                artifact_sha256, summary_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(diagnosis_id) DO UPDATE SET
                loop_id=excluded.loop_id,
                candidate_signature=excluded.candidate_signature,
                holdout_start=excluded.holdout_start,
                holdout_end=excluded.holdout_end,
                candidate_run_id=excluded.candidate_run_id,
                baseline_run_id=excluded.baseline_run_id,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                summary_json=excluded.summary_json
            """,
            (
                diagnosis_id,
                research.get("loop_id"),
                best.get("candidate_signature") or best.get("signature"),
                start_date,
                end_date,
                candidate_report.get("run_id"),
                baseline_report.get("run_id"),
                str(written),
                payload["artifact_sha256"],
                canonical_json_bytes(
                    {
                        "candidate_metrics": payload["candidate_metrics"],
                        "baseline_metrics": payload["baseline_metrics"],
                        "findings": payload["findings"],
                    },
                    trailing_newline=False,
                ).decode("utf-8"),
                _utc_now(),
            ),
        )
    return payload


__all__ = [
    "DEFAULT_BASELINE_CANDIDATE",
    "DEFAULT_BASELINE_CONFIG",
    "DEFAULT_DIAGNOSIS_OUTPUT_DIR",
    "DEFAULT_DIAGNOSIS_REPORT_DIR",
    "ResearchDiagnosisError",
    "SCHEMA_VERSION",
    "build_diagnosis",
    "build_market_proxy",
    "initialize_diagnosis_schema",
    "load_industry_map",
    "run_research_diagnosis",
]
