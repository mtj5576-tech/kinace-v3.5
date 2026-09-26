"""Deterministic, sandbox-only adversarial gates for execution-framework fixtures."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import PROJECT_ROOT
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .holdout_access import (
    HoldoutConsumedError,
    SANDBOX_TRACK,
    open_holdout_access,
)
from .portfolio_backtest import (
    PortfolioBacktestError,
    validate_snapshot_manifest_for_research,
)
from .research_statistics import analyze_return_series
from .synthetic_sandbox import (
    SandboxPathError,
    initialize_sandbox,
    resolve_sandbox_paths,
    sandbox_run_id,
    verify_sandbox,
    write_sandbox_run_manifest,
)
from .trial_backtest import (
    TrialBacktestError,
    normalize_market_bars,
    simulate_trial,
)


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "kinace_v3_adversarial_suite_results"
FIXTURE_ARTIFACT_TYPE = "kinace_v3_adversarial_fixture_set"
TRACK = "sandbox"
DEFAULT_FIXTURE_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "adversarial_execution_framework.json"
)
DEFAULT_RESULT_PATH = (
    PROJECT_ROOT
    / "reports"
    / "execution_framework"
    / "adversarial"
    / "adversarial-results.json"
)
DEFAULT_SEED = 20260926

REQUIRED_FIXTURE_IDS = (
    "historical_industry_leakage",
    "malformed_market_bars",
    "invalid_ohlc_or_status",
    "terminal_unsellable_holding",
    "adjusted_price_drift",
    "benchmark_return_above_50pct",
    "short_sample_high_sharpe",
    "trial_ledger_or_budget_violation",
    "repeated_holdout_read",
    "formal_sandbox_crossover",
    "nondeterministic_gate_decision",
    "pure_noise_candidate",
)

EXPECTED_CASE_REASON_SETS = {
    "historical_industry_leakage": {"historical_industry_leakage"},
    "invalid_market_bars": {
        "market_bars_out_of_order",
        "market_bars_duplicate",
        "market_bars_non_finite",
    },
    "invalid_ohlc_or_status": {"ohlc_inconsistent", "ohlc_status_missing"},
    "terminal_unsellable_holding": {"terminal_unsellable_holding"},
    "adjusted_price_drift": {"adjusted_price_drift"},
    "benchmark_return_above_50pct": {"benchmark_return_above_50pct"},
    "short_sample_high_sharpe": {"short_sample_high_sharpe"},
    "trial_ledger_or_budget_violation": {
        "unregistered_trial",
        "trial_budget_exceeded",
    },
    "holdout_consumed": {"holdout_consumed"},
    "formal_sandbox_crossover": {
        "formal_sandbox_path_crossover",
        "formal_sandbox_trial_id_crossover",
    },
    "nondeterministic_gate_decision": {"nondeterministic_gate_decision"},
    "pure_noise_candidate": {"pure_noise_candidate"},
}


class AdversarialSuiteError(ValueError):
    """Raised when adversarial fixtures or their sandbox execution are invalid."""


def _resolve(path: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(path)))
    return Path(expanded).resolve(strict=False)


def _load_fixture_set(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = _resolve(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdversarialSuiteError(f"adversarial fixture set is missing: {target}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AdversarialSuiteError(f"cannot load adversarial fixture set: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdversarialSuiteError("adversarial fixture set must be a JSON object")
    if payload.get("artifact_type") != FIXTURE_ARTIFACT_TYPE:
        raise AdversarialSuiteError("adversarial fixture set has the wrong artifact_type")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise AdversarialSuiteError("adversarial fixture set has an unsupported schema_version")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list):
        raise AdversarialSuiteError("adversarial fixture set must contain a fixtures list")

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(fixtures):
        if not isinstance(value, Mapping):
            raise AdversarialSuiteError(f"adversarial fixture {index} must be an object")
        fixture = dict(value)
        fixture_id = str(fixture.get("id") or "").strip()
        expected = str(fixture.get("expected_rejection_reason") or "").strip()
        if not fixture_id or fixture_id in seen:
            raise AdversarialSuiteError("adversarial fixture IDs must be non-empty and unique")
        if not expected:
            raise AdversarialSuiteError(
                f"adversarial fixture {fixture_id} must declare a rejection reason"
            )
        if not isinstance(fixture.get("input"), Mapping):
            raise AdversarialSuiteError(
                f"adversarial fixture {fixture_id} input must be an object"
            )
        seen.add(fixture_id)
        fixture["id"] = fixture_id
        fixture["expected_rejection_reason"] = expected
        fixture["input"] = dict(fixture["input"])
        normalized.append(fixture)

    missing = [fixture_id for fixture_id in REQUIRED_FIXTURE_IDS if fixture_id not in seen]
    if missing:
        raise AdversarialSuiteError(
            "adversarial fixture set is missing required injections: " + ", ".join(missing)
        )
    payload["fixtures"] = normalized
    return payload


def _case(
    name: str,
    expected_reason: str,
    *,
    rejected: bool,
    observed_reason: str | None,
    detail: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "expected_rejection_reason": expected_reason,
        "rejected": bool(rejected),
        "observed_rejection_reason": observed_reason,
        "matched": bool(rejected and observed_reason == expected_reason),
        "detail": detail,
        "evidence": dict(evidence or {}),
    }


def _exception_case(
    name: str,
    expected_reason: str,
    exception_type: type[BaseException],
    action: Callable[[], Any],
    *,
    match: str | None = None,
) -> dict[str, Any]:
    try:
        action()
    except exception_type as exc:
        detail = str(exc)
        matched = match is None or any(
            candidate.casefold() in detail.casefold()
            for candidate in match.split("|")
            if candidate
        )
        return _case(
            name,
            expected_reason,
            rejected=matched,
            observed_reason=expected_reason if matched else "unexpected_rejection_reason",
            detail=detail,
        )
    except Exception as exc:  # pragma: no cover - defensive fixture diagnostics
        return _case(
            name,
            expected_reason,
            rejected=False,
            observed_reason="unexpected_exception",
            detail=f"{type(exc).__name__}: {exc}",
        )
    return _case(
        name,
        expected_reason,
        rejected=False,
        observed_reason=None,
        detail="injection was not rejected",
    )


def _bar(
    trade_date: str,
    open_price: float = 10.0,
    high: float = 10.5,
    low: float = 9.5,
    close: float = 10.2,
    *,
    volume: float = 10_000.0,
    is_suspended: bool = False,
    is_limit_up: bool = False,
    is_limit_down: bool = False,
) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "is_suspended": is_suspended,
        "is_limit_up": is_limit_up,
        "is_limit_down": is_limit_down,
    }


def _historical_industry_leakage(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw_case in fixture["input"]["cases"]:
        case = dict(raw_case)
        payload = {
            "artifact_type": "kinace_v3_history_feature_build",
            "snapshot_mode": str(case["snapshot_mode"]),
            "pit_ready": bool(case["pit_ready"]),
            "industry_lookahead_safe": bool(case["industry_lookahead_safe"]),
            "industry_source": str(case["industry_source"]),
            "pit_policy_hash": case.get("pit_policy_hash"),
            "industry_coverage": case.get("industry_coverage"),
            "price_basis": "adjusted",
            "execution_price_basis": "raw",
        }
        results.append(
            _exception_case(
                str(case["name"]),
                "historical_industry_leakage",
                PortfolioBacktestError,
                lambda payload=payload: validate_snapshot_manifest_for_research(payload),
                match="pit-ready|industry_lookahead_safe|required pit",
            )
        )
    return results


def _malformed_market_bars(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]

    def out_of_order() -> None:
        rows = [dict(row) for row in input_payload["out_of_order_rows"]]
        dates = [str(row["trade_date"]) for row in rows]
        if dates != sorted(dates):
            raise TrialBacktestError("market bars are not in canonical ascending date order")
        normalize_market_bars({"price_basis": "raw", "bars": {"000001": rows}})

    duplicate_rows = [dict(row) for row in input_payload["duplicate_rows"]]
    non_finite_rows = [dict(row) for row in input_payload["non_finite_rows"]]
    non_finite_rows[-1]["volume"] = float("nan")
    return [
        _exception_case(
            "out_of_order",
            "market_bars_out_of_order",
            TrialBacktestError,
            out_of_order,
            match="ascending date order",
        ),
        _exception_case(
            "duplicate",
            "market_bars_duplicate",
            TrialBacktestError,
            lambda: normalize_market_bars(
                {"price_basis": "raw", "bars": {"000001": duplicate_rows}}
            ),
            match="duplicate market bar",
        ),
        _exception_case(
            "non_finite",
            "market_bars_non_finite",
            TrialBacktestError,
            lambda: normalize_market_bars(
                {"price_basis": "raw", "bars": {"000001": non_finite_rows}}
            ),
            match="finite number",
        ),
    ]


def _invalid_ohlc_or_status(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    inconsistent = [dict(row) for row in input_payload["inconsistent_ohlc_rows"]]
    missing_status = [dict(row) for row in input_payload["missing_status_rows"]]
    missing_status[-1].pop(str(input_payload["missing_status_field"]), None)
    return [
        _exception_case(
            "inconsistent_ohlc",
            "ohlc_inconsistent",
            TrialBacktestError,
            lambda: normalize_market_bars(
                {"price_basis": "raw", "bars": {"000001": inconsistent}}
            ),
            match="high is inconsistent|low is inconsistent",
        ),
        _exception_case(
            "missing_status",
            "ohlc_status_missing",
            TrialBacktestError,
            lambda: normalize_market_bars(
                {"price_basis": "raw", "bars": {"000001": missing_status}}
            ),
            match="must be a boolean",
        ),
    ]


def _terminal_unsellable_holding(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case_value in fixture["input"]["cases"]:
        case = str(case_value)
        bars = {
            "000001": [
                _bar("2024-01-01", 10.0, 10.2, 9.8, 10.0),
                _bar("2024-01-02", 10.0, 10.2, 9.8, 10.0),
                _bar("2024-01-03", 10.0, 10.6, 9.9, 10.5),
                _bar("2024-01-04", 11.0, 11.2, 10.8, 11.0),
            ]
        }
        scores = [{"decision_date": "2024-01-02", "code": "000001", "score": 1.0}]
        if case == "missing_bar":
            scores.append(
                {"decision_date": "2024-01-05", "code": "000001", "score": -1.0}
            )
        else:
            bars["000001"][-1].update(
                {
                    "is_suspended": case == "suspended",
                    "is_limit_down": case == "limit_down",
                    "volume": 0.0 if case == "zero_volume" else 10_000.0,
                }
            )
        try:
            result = simulate_trial(
                scores=scores,
                bars=bars,
                config={
                    "initial_cash": 100_000.0,
                    "position_size_pct": 0.5,
                    "lot_size": 1,
                    "max_hold_sessions": 10,
                    "commission_rate": 0.0,
                    "min_commission": 0.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 0.0,
                    "max_volume_participation": 1.0,
                    "stop_atr_multiple": 100.0,
                    "target_atr_multiple": 100.0,
                },
            )
        except TrialBacktestError as exc:
            results.append(
                _case(
                    case,
                    "terminal_unsellable_holding",
                    rejected=False,
                    observed_reason="unexpected_exception",
                    detail=str(exc),
                )
            )
            continue
        positions = result.get("open_positions") if isinstance(result, Mapping) else None
        observed_reason = (
            str(positions[0].get("unsellable_reason"))
            if isinstance(positions, list)
            and positions
            and isinstance(positions[0], Mapping)
            else None
        )
        results.append(
            _case(
                case,
                "terminal_unsellable_holding",
                rejected=observed_reason
                in {"suspended", "limit_down", "zero_volume", "missing_bar"},
                observed_reason="terminal_unsellable_holding" if observed_reason else None,
                detail=f"terminal holding reason={observed_reason}",
                evidence={"portfolio_unsellable_reason": observed_reason},
            )
        )
    return results


def _adjusted_price_drift(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    baseline = normalize_market_bars(
        {"price_basis": "adjusted", "bars": input_payload["baseline_bars"]},
        price_basis="adjusted",
        require_execution_flags=False,
        require_volume=False,
    )
    extended = normalize_market_bars(
        {"price_basis": "adjusted", "bars": input_payload["extended_bars"]},
        price_basis="adjusted",
        require_execution_flags=False,
        require_volume=False,
    )
    drifted: list[dict[str, Any]] = []
    tolerance = float(input_payload.get("tolerance", 1e-12))
    for code, baseline_rows in baseline.items():
        extended_by_date = {row["trade_date"]: row for row in extended.get(code, [])}
        for baseline_row in baseline_rows:
            extended_row = extended_by_date.get(baseline_row["trade_date"])
            if extended_row is None:
                continue
            for field in ("open", "high", "low", "close"):
                before = float(baseline_row[field])
                after = float(extended_row[field])
                if not math.isclose(
                    before, after, rel_tol=tolerance, abs_tol=tolerance
                ):
                    drifted.append(
                        {
                            "code": code,
                            "trade_date": baseline_row["trade_date"],
                            "field": field,
                            "before": before,
                            "after": after,
                        }
                    )
    basis_changed = str(input_payload["baseline_adjustment_basis_date"]) != str(
        input_payload["extended_adjustment_basis_date"]
    )
    rejected = bool(drifted or basis_changed)
    return [
        _case(
            "extension_rebase",
            "adjusted_price_drift",
            rejected=rejected,
            observed_reason="adjusted_price_drift" if rejected else None,
            detail=f"basis_changed={basis_changed}, overlapping_bar_drifts={len(drifted)}",
            evidence={
                "basis_changed": basis_changed,
                "first_drift": drifted[0] if drifted else None,
                "drift_count": len(drifted),
            },
        )
    ]


def _benchmark_return_above_50pct(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    try:
        result = simulate_trial(
            scores=[
                {
                    "decision_date": str(input_payload["decision_date"]),
                    "code": str(input_payload["signal_code"]),
                    "score": 1.0,
                }
            ],
            bars=input_payload["bars"],
            config=input_payload["config"],
        )
    except TrialBacktestError as exc:
        return [
            _case(
                "raw_equal_weight_benchmark",
                "benchmark_return_above_50pct",
                rejected=False,
                observed_reason="unexpected_exception",
                detail=str(exc),
            )
        ]
    metrics = result["metrics"]
    raw_curve = metrics.get("benchmark_raw_curve") or []
    raw_returns = [
        float(point.get("benchmark_return") or 0.0)
        for point in raw_curve
        if isinstance(point, Mapping)
    ]
    observed_max = max(raw_returns, default=0.0)
    sanitized_total = float(metrics.get("benchmark_sanitized_total_return") or 0.0)
    exclusions = metrics.get("benchmark_exclusions") or []
    rejected = observed_max > 0.50 and sanitized_total <= 0.50 and bool(exclusions)
    return [
        _case(
            "raw_equal_weight_benchmark",
            "benchmark_return_above_50pct",
            rejected=rejected,
            observed_reason="benchmark_return_above_50pct" if rejected else None,
            detail=(
                f"max_raw_return={observed_max:.6f}, "
                f"sanitized_total_return={sanitized_total:.6f}, "
                f"exclusions={len(exclusions)}"
            ),
            evidence={
                "max_raw_return": observed_max,
                "benchmark_total_return": metrics.get("benchmark_total_return"),
                "sanitized_total_return": sanitized_total,
                "exclusion_count": len(exclusions),
            },
        )
    ]


def _short_sample_high_sharpe(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    settings = fixture["input"]
    for raw_case in settings["cases"]:
        case = dict(raw_case)
        analysis = analyze_return_series(
            case["returns"],
            trade_count=int(settings["trade_count"]),
            min_observation_sessions=int(settings["min_observation_sessions"]),
            min_trade_count=int(settings["min_trade_count"]),
            target_sharpe=float(settings["target_sharpe"]),
            N_trials=int(settings["N_trials"]),
            seed=int(settings["statistics_seed"]),
        )
        unavailable = str(analysis.get("unavailable_reason") or "")
        rejected = (
            analysis.get("status") == "unavailable"
            and analysis.get("observed", {}).get("annualized_sharpe") is None
            and "minimum_observation_sessions_not_met" in unavailable
        )
        results.append(
            _case(
                str(case["name"]),
                "short_sample_high_sharpe",
                rejected=rejected,
                observed_reason="short_sample_high_sharpe" if rejected else None,
                detail=f"status={analysis.get('status')}, unavailable_reason={unavailable}",
                evidence={
                    "sessions": analysis.get("sample_requirements", {}).get(
                        "available_sessions"
                    ),
                    "required_sessions": analysis.get("sample_requirements", {}).get(
                        "required_sessions"
                    ),
                    "annualized_sharpe": analysis.get("observed", {}).get(
                        "annualized_sharpe"
                    ),
                },
            )
        )
    return results


def _trial_ledger_or_budget_violation(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    registered = {str(value) for value in input_payload["registered_trial_ids"]}
    ledger = [str(value) for value in input_payload["ledger_trial_ids"]]
    budget = int(input_payload["trial_budget"])
    unregistered = [trial_id for trial_id in ledger if trial_id not in registered]
    over_budget = len(ledger) > budget
    return [
        _case(
            "unregistered_trial",
            "unregistered_trial",
            rejected=bool(unregistered),
            observed_reason="unregistered_trial" if unregistered else None,
            detail=f"unregistered_trials={unregistered}",
        ),
        _case(
            "budget_overrun",
            "trial_budget_exceeded",
            rejected=over_budget,
            observed_reason="trial_budget_exceeded" if over_budget else None,
            detail=f"ledger_size={len(ledger)}, trial_budget={budget}",
        ),
    ]


def _repeated_holdout_read(
    fixture: Mapping[str, Any],
    layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    fixture_root = layout["outputs"] / "adversarial" / str(fixture["id"])
    registry_path = fixture_root / "holdout-access-registry.json"
    if registry_path.exists():
        registry_path.unlink()
    fixture_root.mkdir(parents=True, exist_ok=True)
    open_holdout_access(
        str(input_payload["candidate"]),
        str(input_payload["protocol"]),
        input_payload["window"],
        eligible=True,
        registry_path=registry_path,
        track=SANDBOX_TRACK,
        timestamp=str(input_payload["timestamp"]),
    )
    return [
        _exception_case(
            "second_open",
            "holdout_consumed",
            HoldoutConsumedError,
            lambda: open_holdout_access(
                str(input_payload["second_candidate"]),
                str(input_payload["protocol"]),
                input_payload["window"],
                eligible=True,
                registry_path=registry_path,
                track=SANDBOX_TRACK,
                timestamp=str(input_payload["timestamp"]),
            ),
            match="holdout_consumed",
        )
    ]


def _formal_sandbox_crossover(
    fixture: Mapping[str, Any],
    layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    sandbox_root = (
        layout["root"] / "outputs" / "adversarial" / str(fixture["id"])
    )
    formal_paths = {
        "database": layout["root"] / "formal-database.sqlite3",
        "snapshots": layout["root"] / "formal-snapshots",
        "trial_ledger": layout["root"] / "formal-ledger.jsonl",
        "reports": sandbox_root / "reports",
        "holdout_registry": layout["root"] / "formal-holdout-registry.json",
    }
    formal_trial_ids = {str(value) for value in input_payload["formal_trial_ids"]}
    sandbox_trial_ids = [str(value) for value in input_payload["sandbox_trial_ids"]]
    crossover_ids = [
        trial_id
        for trial_id in sandbox_trial_ids
        if trial_id in formal_trial_ids or not trial_id.startswith("sandbox-")
    ]
    return [
        _exception_case(
            "path_crossover",
            "formal_sandbox_path_crossover",
            SandboxPathError,
            lambda: resolve_sandbox_paths(
                root=sandbox_root,
                formal_paths=formal_paths,
                protected_roots=(),
            ),
            match="overlap|contain|formal",
        ),
        _case(
            "trial_id_crossover",
            "formal_sandbox_trial_id_crossover",
            rejected=bool(crossover_ids),
            observed_reason=(
                "formal_sandbox_trial_id_crossover" if crossover_ids else None
            ),
            detail=f"crossover_trial_ids={crossover_ids}",
        ),
    ]


def _nondeterministic_gate_decision(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    by_fingerprint: dict[str, set[str]] = {}
    for raw_observation in input_payload["observations"]:
        observation = dict(raw_observation)
        fingerprint = str(observation["fingerprint"])
        by_fingerprint.setdefault(fingerprint, set()).add(str(observation["decision"]))
    inconsistent = {
        fingerprint: sorted(decisions)
        for fingerprint, decisions in sorted(by_fingerprint.items())
        if len(decisions) > 1
    }

    first = analyze_return_series(
        input_payload["determinism_control_returns"],
        min_observation_sessions=0,
        min_trade_count=0,
        target_sharpe=1.0,
        seed=seed,
    )
    second = analyze_return_series(
        input_payload["determinism_control_returns"],
        min_observation_sessions=0,
        min_trade_count=0,
        target_sharpe=1.0,
        seed=seed,
    )
    control_hashes = [
        sha256_bytes(canonical_json_bytes(first, trailing_newline=False)),
        sha256_bytes(canonical_json_bytes(second, trailing_newline=False)),
    ]
    rejected = bool(inconsistent) and control_hashes[0] == control_hashes[1]
    return [
        _case(
            "same_fingerprint_divergent_decision",
            "nondeterministic_gate_decision",
            rejected=rejected,
            observed_reason="nondeterministic_gate_decision" if rejected else None,
            detail=f"inconsistent_fingerprints={len(inconsistent)}",
            evidence={
                "inconsistent_decisions": inconsistent,
                "determinism_control_hashes": control_hashes,
            },
        )
    ]


def _pure_noise_candidate(
    fixture: Mapping[str, Any],
    _layout: Mapping[str, Path],
    _seed: int,
) -> list[dict[str, Any]]:
    input_payload = fixture["input"]
    information_coefficients = [
        float(value) for value in input_payload["information_coefficients"]
    ]
    fold_sharpes = [float(value) for value in input_payload["fold_sharpes"]]
    mean_abs_ic = (
        sum(abs(value) for value in information_coefficients)
        / len(information_coefficients)
        if information_coefficients
        else 0.0
    )
    positive_fold_ratio = (
        sum(1 for value in fold_sharpes if value > 0) / len(fold_sharpes)
        if fold_sharpes
        else 0.0
    )
    minimum_mean_abs_ic = float(input_payload["minimum_mean_abs_ic"])
    minimum_positive_fold_ratio = float(input_payload["minimum_positive_fold_ratio"])
    rejected = (
        mean_abs_ic < minimum_mean_abs_ic
        and positive_fold_ratio < minimum_positive_fold_ratio
    )
    return [
        _case(
            "no_economic_signal",
            "pure_noise_candidate",
            rejected=rejected,
            observed_reason="pure_noise_candidate" if rejected else None,
            detail=(
                f"mean_abs_ic={mean_abs_ic:.6f}, "
                f"positive_fold_ratio={positive_fold_ratio:.6f}"
            ),
            evidence={
                "mean_abs_ic": round(mean_abs_ic, 12),
                "minimum_mean_abs_ic": minimum_mean_abs_ic,
                "positive_fold_ratio": round(positive_fold_ratio, 12),
                "minimum_positive_fold_ratio": minimum_positive_fold_ratio,
            },
        )
    ]


_FIXTURE_HANDLERS: dict[
    str,
    Callable[[Mapping[str, Any], Mapping[str, Path], int], list[dict[str, Any]]],
] = {
    "historical_industry_leakage": _historical_industry_leakage,
    "malformed_market_bars": _malformed_market_bars,
    "invalid_ohlc_or_status": _invalid_ohlc_or_status,
    "terminal_unsellable_holding": _terminal_unsellable_holding,
    "adjusted_price_drift": _adjusted_price_drift,
    "benchmark_return_above_50pct": _benchmark_return_above_50pct,
    "short_sample_high_sharpe": _short_sample_high_sharpe,
    "trial_ledger_or_budget_violation": _trial_ledger_or_budget_violation,
    "repeated_holdout_read": _repeated_holdout_read,
    "formal_sandbox_crossover": _formal_sandbox_crossover,
    "nondeterministic_gate_decision": _nondeterministic_gate_decision,
    "pure_noise_candidate": _pure_noise_candidate,
}


def _execute_fixture(
    fixture: Mapping[str, Any],
    layout: Mapping[str, Path],
    *,
    seed: int,
) -> dict[str, Any]:
    fixture_id = str(fixture["id"])
    expected = str(fixture["expected_rejection_reason"])
    expected_case_reasons = {
        str(value) for value in fixture.get("case_rejection_reasons", [expected])
    }
    expectation_valid = (
        EXPECTED_CASE_REASON_SETS.get(expected) == expected_case_reasons
    )
    handler = _FIXTURE_HANDLERS.get(fixture_id)
    if handler is None:
        raise AdversarialSuiteError(f"no adversarial handler for fixture: {fixture_id}")
    cases = handler(fixture, layout, seed)
    mismatches = [
        case
        for case in cases
        if not case["matched"]
        or case["expected_rejection_reason"] not in expected_case_reasons
    ]
    rejected = expectation_valid and bool(cases) and not mismatches
    reasons = sorted(
        {
            str(case["observed_rejection_reason"])
            for case in cases
            if case["observed_rejection_reason"] is not None
        }
    )
    return {
        "fixture_id": fixture_id,
        "injection": str(fixture.get("injection") or fixture_id),
        "expected_rejection_reason": expected,
        "expected_case_rejection_reasons": sorted(expected_case_reasons),
        "actual_rejection_reason": (
            expected if rejected else (reasons[0] if reasons else None)
        ),
        "observed_rejection_reasons": reasons,
        "rejected": rejected,
        "status": "rejected" if rejected else "false_negative",
        "case_count": len(cases),
        "cases": cases,
    }


def run_adversarial_suite(
    *,
    root: str | os.PathLike[str],
    fixtures_path: str | os.PathLike[str] = DEFAULT_FIXTURE_PATH,
    output_path: str | os.PathLike[str] | None = DEFAULT_RESULT_PATH,
    seed: int = DEFAULT_SEED,
    formal_paths: Mapping[str, str | os.PathLike[str]] | None = None,
    protected_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> dict[str, Any]:
    """Execute every adversarial fixture inside one isolated synthetic sandbox."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise AdversarialSuiteError("adversarial suite seed must be an integer")
    fixtures_target = _resolve(fixtures_path)
    initialized = initialize_sandbox(
        root=root,
        formal_paths=formal_paths,
        protected_roots=protected_roots,
    )
    layout = {name: Path(value) for name, value in initialized["paths"].items()}
    layout["root"] = Path(initialized["root"])
    fixture_payload = _load_fixture_set(fixtures_target)
    fixture_hash = sha256_file(fixtures_target)

    results: list[dict[str, Any]] = []
    for fixture in fixture_payload["fixtures"]:
        result = _execute_fixture(fixture, layout, seed=seed)
        run_id = sandbox_run_id(
            layout["root"],
            purpose=f"adversarial-{fixture['id']}",
            identity={"fixture_set_sha256": fixture_hash, "seed": seed},
        )
        manifest_path = write_sandbox_run_manifest(
            root=layout["root"],
            run_id=run_id,
            payload={
                "artifact_type": "kinace_v3_adversarial_fixture_result",
                "fixture_id": fixture["id"],
                "injection": fixture.get("injection"),
                "expected_rejection_reason": fixture["expected_rejection_reason"],
                "actual_rejection_reason": result["actual_rejection_reason"],
                "rejected": result["rejected"],
                "status": result["status"],
                "case_rejection_reasons": result["observed_rejection_reasons"],
                "seed": seed,
            },
            formal_paths=formal_paths,
            protected_roots=protected_roots,
        )
        result["sandbox_run_id"] = run_id
        result["sandbox_manifest_path"] = str(manifest_path)
        results.append(result)

    injected_count = len(results)
    rejected_count = sum(1 for result in results if result["rejected"])
    false_negative_count = injected_count - rejected_count
    case_false_negative_count = sum(
        1
        for result in results
        for case in result["cases"]
        if not case["matched"]
    )
    verification = verify_sandbox(
        root=layout["root"],
        formal_paths=formal_paths,
        protected_roots=protected_roots,
    )
    status = (
        "passed"
        if injected_count == len(REQUIRED_FIXTURE_IDS)
        and rejected_count == injected_count
        and false_negative_count == 0
        and case_false_negative_count == 0
        and verification["status"] == "passed"
        else "failed"
    )
    identity = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "track": TRACK,
        "status": status,
        "fixture_set_sha256": fixture_hash,
        "seed": seed,
        "injected_count": injected_count,
        "rejected_count": rejected_count,
        "false_negative_count": false_negative_count,
        "case_false_negative_count": case_false_negative_count,
        "required_fixture_ids": list(REQUIRED_FIXTURE_IDS),
        "fixtures": results,
        "sandbox": {
            "track": TRACK,
            "root": str(layout["root"]),
            "sandbox_id": initialized["sandbox_id"],
            "run_id_prefix": initialized["run_id_prefix"],
            "verification": verification,
        },
        "production_ready": False,
        "production_changed": False,
        "real_money_orders_enabled": False,
    }
    payload = dict(identity)
    payload["result_sha256"] = sha256_bytes(
        canonical_json_bytes(identity, trailing_newline=False)
    )
    target = _resolve(output_path or DEFAULT_RESULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    returned = dict(payload)
    returned["artifact_path"] = str(target)
    returned["artifact_sha256"] = sha256_file(target)
    return returned


__all__ = [
    "ARTIFACT_TYPE",
    "AdversarialSuiteError",
    "DEFAULT_FIXTURE_PATH",
    "DEFAULT_RESULT_PATH",
    "DEFAULT_SEED",
    "FIXTURE_ARTIFACT_TYPE",
    "REQUIRED_FIXTURE_IDS",
    "SCHEMA_VERSION",
    "TRACK",
    "run_adversarial_suite",
]
