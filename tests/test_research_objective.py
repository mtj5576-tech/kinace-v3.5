import json
import sqlite3

from kinace_v3.research_objective import (
    build_objective_policy_audit,
    run_objective_policy_audit,
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


def _metrics(sharpe: float, total_return: float) -> dict:
    return {
        "equity_basis": "mark_to_market",
        "total_return": total_return,
        "annualized_return": total_return,
        "excess_return": total_return / 2.0,
        "sharpe": sharpe,
        "sortino": sharpe * 1.1,
        "max_drawdown": 0.05,
        "profit_factor": 1.2,
        "win_rate": 0.52,
        "turnover": 4.0,
        "trading_days": 40,
        "trade_count": 40,
    }


def _loop_report() -> dict:
    folds = [
        {
            "fold": 0,
            "training": {"trained_weights": _weights(0.80)},
            "metrics": _metrics(1.2, 0.10),
            "components": {
                "total_return": 0.10,
                "annualized_return": 0.10,
                "sharpe": 0.24,
            },
        },
        {
            "fold": 1,
            "training": {"trained_weights": _weights(0.20)},
            "metrics": _metrics(0.6, 0.05),
            "components": {
                "total_return": 0.05,
                "annualized_return": 0.05,
                "sharpe": 0.12,
            },
        },
        {
            "fold": 2,
            "training": {"trained_weights": _weights(0.40)},
            "metrics": _metrics(2.0, 0.20),
            "components": {
                "total_return": 0.20,
                "annualized_return": 0.20,
                "sharpe": 0.40,
            },
        },
    ]
    best = {
        "signature": "candidate-a",
        "folds": folds,
        "final_training": {"trained_weights": _weights(0.30)},
    }
    return {
        "artifact_type": "kinace_v3_automated_research_loop",
        "loop_id": "loop-a",
        "best_candidate": best,
        "rounds": [{"round": 0, "candidates": [best]}],
        "holdout": {"metrics": _metrics(0.9, 0.04)},
        "objective": {
            "metric": "kinace_v3_composite_backtest_objective",
            "weights": {
                "total_return": 1.0,
                "annualized_return": 0.25,
                "sharpe": 0.20,
            },
            "hard_gates": {
                "min_trade_count": 30,
                "max_drawdown": 0.60,
                "min_profit_factor": 1.0,
                "min_total_return": 0.0,
                "min_win_rate": 0.0,
            },
        },
    }


def test_objective_policy_selects_net_sharpe_and_reports_redundancy():
    result = build_objective_policy_audit(_loop_report())

    assert result["status"] == "completed"
    assert result["objective"]["primary"]["metric"] == "net_sharpe"
    assert result["objective"]["policy"]["holdout_used_for_selection"] is False
    assert result["objective"]["report_only"]["max_feature_weight"]["binding"] is False
    assert result["max_weight_report_only"]["status"] == "report_only"
    assert result["max_weight_report_only"]["max"] == 0.8
    assert any(
        pair["left"] == "total_return"
        and pair["right"] == "annualized_return"
        and pair["spearman"] == 1.0
        for pair in result["correlation_diagnostics"]["high_redundancy_pairs"]
    )
    assert result["production_changed"] is False


def test_objective_policy_artifact_and_database_record_are_written(tmp_path):
    loop_path = tmp_path / "loop.json"
    loop_path.write_text(json.dumps(_loop_report()), encoding="utf-8")
    database = tmp_path / "v3.sqlite3"

    result = run_objective_policy_audit(
        database,
        loop_path,
        output_path=tmp_path / "objective.json",
        markdown_path=tmp_path / "objective.md",
    )

    assert result["artifact_path"] == str((tmp_path / "objective.json").resolve())
    assert (tmp_path / "objective.json").is_file()
    assert (tmp_path / "objective.md").is_file()
    with sqlite3.connect(str(database)) as connection:
        row = connection.execute(
            "SELECT primary_metric, max_feature_weight_binding "
            "FROM research_objective_policy_runs"
        ).fetchone()
    assert row == ("net_sharpe", 0)
