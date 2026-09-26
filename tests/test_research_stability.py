from __future__ import annotations

import json

from kinace_v3.research_stability import (
    ResearchStabilityError,
    _compare_reports,
    _candidate_holdout_report,
    _candidate_window_reports,
    _paired_delta_statistics,
    normalize_stability_config,
    objective_with_fold_trade_minimum,
    run_research_stability,
    summarize_candidate_trade_count,
    summarize_weight_stability,
)


def _weights(gap: float) -> dict[str, float]:
    return {
        "rel_sector_strength": 0.1,
        "close_location_5": 0.1,
        "volume_ratio": 0.1,
        "log_amount": 0.1,
        "turnover": 0.0,
        "proximity_20d_high": 0.1,
        "ret_5": 0.1,
        "gap": gap,
    }


def _report(
    *,
    run_id: str,
    total_return: float,
    profit_factor: float,
    drawdown: float,
    trade_count: int = 40,
) -> dict:
    return {
        "run_id": run_id,
        "date_range": {"start": "2026-01-02", "end": "2026-01-31"},
        "snapshot_manifest": {"snapshot_set_id": "snapshot-set"},
        "config": {
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "transfer_fee_rate": 0.00001,
            "slippage_bps": 5.0,
        },
        "metrics": {
            "equity_basis": "mark_to_market",
            "total_return": total_return,
            "annualized_return": total_return,
            "excess_return": total_return,
            "sharpe": 0.0,
            "sortino": 0.0,
            "profit_factor": profit_factor,
            "max_drawdown": drawdown,
            "calmar": 0.0,
            "win_rate": 0.5,
            "turnover": 1.0,
            "trading_days": 20,
            "trade_count": trade_count,
        },
        "trades": [{"code": "000001"}],
    }


def test_normalize_stability_config_rejects_unknown_keys():
    try:
        normalize_stability_config({"unknown": True})
    except ResearchStabilityError as exc:
        assert "unknown stability configuration keys" in str(exc)
    else:
        raise AssertionError("unknown stability config should fail")


def test_normalize_stability_config_rejects_wrong_trade_count_policy_version():
    try:
        normalize_stability_config(
            {"trade_count_policy_version": "unregistered-policy"}
        )
    except ResearchStabilityError as exc:
        assert "unsupported trade_count_policy_version" in str(exc)
    else:
        raise AssertionError("unknown trade-count policy version should fail")


def test_candidate_trade_count_policy_uses_candidate_aggregate():
    result = summarize_candidate_trade_count([8, 10, 12], {})

    assert result["candidate_trade_count"] == 30
    assert result["active_folds"] == 3
    assert result["passed"] is True
    assert result["scope"] == "walk_forward_candidate_aggregate"


def test_candidate_trade_count_policy_requires_all_folds_active():
    result = summarize_candidate_trade_count([0, 15, 15], {})

    assert result["candidate_trade_count"] == 30
    assert result["active_folds"] == 2
    assert result["checks"]["candidate_trade_count"] is True
    assert result["checks"]["active_fold_rate"] is False
    assert result["passed"] is False


def test_fold_objective_replaces_per_fold_minimum_without_mutating_source():
    objective = {
        "constraints": {
            "min_trade_count": {
                "operator": ">=",
                "threshold": 30,
                "binding": True,
                "source": "phase3_risk_policy",
            }
        }
    }

    adjusted = objective_with_fold_trade_minimum(objective, 1)

    assert adjusted["constraints"]["min_trade_count"]["threshold"] == 1
    assert objective["constraints"]["min_trade_count"]["threshold"] == 30


def test_stability_rejects_requalification_of_pre_policy_protocol(tmp_path):
    loop_path = tmp_path / "loop.json"
    loop_path.write_text(
        json.dumps(
            {
                "loop_id": "legacy-loop",
                "best_candidate": {"signature": "legacy-candidate"},
                "evaluation_protocol": {
                    "protocol_identity": {
                        "evaluation_config": {
                            "stability": {"max_feature_weight": 0.75}
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        run_research_stability(
            tmp_path / "v3.sqlite3",
            loop_path,
            snapshot_dir=tmp_path / "snapshots",
        )
    except ResearchStabilityError as exc:
        assert "predates the candidate-level trade-count policy" in str(exc)
    else:
        raise AssertionError("legacy protocol requalification should be rejected")


def test_paired_delta_statistics_reports_bootstrap_interval():
    result = _paired_delta_statistics(
        [0.10, 0.20, -0.05, 0.15],
        bootstrap_iterations=500,
        confidence_level=0.95,
        seed=7,
    )

    interval = result["bootstrap_confidence_interval"]
    assert result["observations"] == 4
    assert interval["lower"] <= result["mean"] <= interval["upper"]
    assert result["sign_test_p_value"] is not None


def test_rolling_stability_excludes_final_holdout(tmp_path):
    fold_path = tmp_path / "fold.json"
    holdout_path = tmp_path / "holdout.json"
    fold_path.write_text(
        json.dumps({"date_range": {"start": "2026-01-01", "end": "2026-01-20"}}),
        encoding="utf-8",
    )
    holdout_path.write_text(
        json.dumps({"date_range": {"start": "2026-02-01", "end": "2026-03-01"}}),
        encoding="utf-8",
    )
    loop_report = {
        "best_candidate": {
            "folds": [{"fold": 0, "portfolio_output_path": str(fold_path)}]
        },
        "holdout": {"portfolio_output_path": str(holdout_path)},
    }

    rolling = _candidate_window_reports(loop_report)
    holdout = _candidate_holdout_report(loop_report)

    assert [item["kind"] for item in rolling] == ["walk_forward_fold"]
    assert holdout["kind"] == "final_holdout"


def test_weight_stability_reports_concentration_without_phase3_gate():
    loop_report = {
        "best_candidate": {
            "folds": [
                {"fold": 0, "training": {"trained_weights": _weights(0.80)}},
                {"fold": 1, "training": {"trained_weights": _weights(0.80)}},
            ],
            "final_training": {"trained_weights": _weights(0.50)},
        }
    }

    result = summarize_weight_stability(
        loop_report,
        max_feature_weight=0.75,
        max_feature_weight_binding=False,
    )

    assert result["status"] == "report_only"
    assert result["binding"] is False
    assert result["budget_exceeded"] is True
    assert result["dominant_feature"] == "gap"
    assert result["max_feature_weight"] == 0.8


def test_weight_stability_can_bind_after_phase4_solver():
    loop_report = {
        "best_candidate": {
            "folds": [
                {"fold": 0, "training": {"trained_weights": _weights(0.80)}},
                {"fold": 1, "training": {"trained_weights": _weights(0.80)}},
            ],
            "final_training": {"trained_weights": _weights(0.50)},
        }
    }

    result = summarize_weight_stability(
        loop_report,
        max_feature_weight=0.75,
        max_feature_weight_binding=True,
    )

    assert result["status"] == "review_required"
    assert result["binding"] is True
    assert result["budget_exceeded"] is True


def test_compare_reports_requires_same_snapshot_and_noninferior_metrics():
    objective = {
        "hard_gates": {
            "min_trade_count": 1,
            "max_drawdown": 1.0,
            "min_profit_factor": 0.0,
            "min_total_return": -1.0,
            "min_win_rate": 0.0,
        }
    }
    candidate = _report(
        run_id="candidate",
        total_return=0.10,
        profit_factor=1.4,
        drawdown=0.12,
    )
    baseline = _report(
        run_id="baseline",
        total_return=-0.05,
        profit_factor=0.9,
        drawdown=0.20,
    )

    result = _compare_reports(
        candidate_report=candidate,
        baseline_report=baseline,
        objective=objective,
        max_drawdown_regression=0.10,
    )

    assert result["verdict"] == "challenger_noninferior"
    assert all(result["checks"].values())
    assert result["metric_delta_challenger_minus_baseline"]["total_return"] == 0.15


def test_compare_reports_applies_candidate_level_trade_scope_per_fold():
    objective = {
        "hard_gates": {
            "min_trade_count": 30,
            "max_drawdown": 1.0,
            "min_profit_factor": 0.0,
            "min_total_return": -1.0,
            "min_win_rate": 0.0,
        }
    }
    candidate = _report(
        run_id="candidate",
        total_return=0.10,
        profit_factor=1.4,
        drawdown=0.12,
        trade_count=10,
    )
    baseline = _report(
        run_id="baseline",
        total_return=-0.05,
        profit_factor=0.9,
        drawdown=0.20,
        trade_count=40,
    )

    result = _compare_reports(
        candidate_report=candidate,
        baseline_report=baseline,
        objective=objective,
        max_drawdown_regression=0.10,
        fold_min_trade_count=1,
    )

    assert result["checks"]["candidate_gate_passed"] is True
    assert result["candidate_objective"]["constraints"]["min_trade_count"][
        "threshold"
    ] == 1
    assert result["verdict"] == "challenger_noninferior"
