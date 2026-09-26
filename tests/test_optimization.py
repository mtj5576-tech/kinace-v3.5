from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kinace_v3.feature_snapshot import FEATURE_NAMES, create_feature_snapshot, write_feature_snapshot
from kinace_v3.optimization import (
    OptimizationError,
    run_parameter_optimization,
    score_portfolio_objective,
    normalize_objective_config,
)


def _model_config(version: str) -> dict:
    return {
        "version": version,
        "mode": "optimization_test",
        "weights": {
            "rel_sector_strength": 0.20,
            "close_location_5": 0.15,
            "volume_ratio": 0.15,
            "log_amount": 0.15,
            "turnover": 0.10,
            "proximity_20d_high": 0.10,
            "ret_5": 0.10,
            "gap": 0.05,
        },
        "penalties": {
            "rsi_overbought": 80.0,
            "kdj_overbought": 100.0,
            "overheat_penalty": 1.0,
            "amplitude_threshold": 0.04,
            "amplitude_multiplier": 4.0,
            "amplitude_max_penalty": 0.35,
        },
    }


def _write_snapshot(snapshot_dir: Path, decision_date: str) -> dict:
    candidates = []
    for index, code in enumerate(("000001", "000002")):
        candidates.append(
            {
                "code": code,
                "raw_inputs": {
                    feature: float(index + 1) + feature_index / 10.0
                    for feature_index, feature in enumerate(FEATURE_NAMES)
                },
                "penalty_inputs": {
                    "rsi": 55.0,
                    "kdj_j": 70.0,
                    "amplitude_5d": 0.01,
                },
                "labels": {"forward_return": 0.02},
                "point_in_time": {"as_of_utc": f"{decision_date}T07:00:00Z"},
            }
        )
    return write_feature_snapshot(
        create_feature_snapshot(
            decision_date=decision_date,
            candidates=candidates,
            model_config=_model_config("snapshot-model"),
            data_gate={"status": "synthetic", "ready": True},
        ),
        snapshot_dir / f"{decision_date}.json",
    )


def _bars() -> dict[str, list[dict]]:
    dates = (
        "2023-12-20",
        "2023-12-21",
        "2023-12-22",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
    )
    result = {}
    for code_offset, code in enumerate(("000001", "000002")):
        result[code] = []
        for index, date in enumerate(dates):
            price = 10.0 + code_offset + index * 0.10
            result[code].append(
                {
                    "trade_date": date,
                    "open": price,
                    "high": price + 0.30,
                    "low": price - 0.10,
                    "close": price + 0.10,
                    "volume": 1_000_000.0,
                    "is_suspended": False,
                    "is_limit_up": False,
                    "is_limit_down": False,
                }
            )
    return result


def _backtest_config(position_size_pct: float) -> dict:
    return {
        "initial_cash": 100_000.0,
        "max_positions": 1,
        "position_size_pct": position_size_pct,
        "min_score": 0.0,
        "max_hold_sessions": 2,
        "stop_atr_multiple": 10.0,
        "target_atr_multiple": 10.0,
    }


def test_primary_net_sharpe_ranks_return_and_enforces_risk_constraints():
    objective = {
        "hard_gates": {
            "min_trade_count": 1,
            "max_drawdown": 0.50,
            "min_profit_factor": 1.0,
            "min_total_return": 0.0,
            "min_win_rate": 0.0,
        }
    }
    base = {
        "equity_basis": "mark_to_market",
        "trade_count": 20,
        "total_return": 0.20,
        "max_drawdown": 0.10,
        "win_rate": 0.55,
        "profit_factor": 1.5,
        "turnover": 5.0,
        "trading_days": 10,
        "sharpe": 1.2,
    }
    better = score_portfolio_objective(base, objective)
    worse = score_portfolio_objective(
        {**base, "sharpe": 0.40, "max_drawdown": 0.40}, objective
    )
    rejected = score_portfolio_objective({**base, "max_drawdown": 0.80}, objective)
    turnover_rejected = score_portfolio_objective(
        {**base, "turnover": 6.0}, objective
    )

    assert better["status"] == "passed"
    assert better["components"] == {"net_sharpe": 1.2}
    assert better["primary_metric"] == "net_sharpe"
    assert better["score"] > worse["score"]
    assert rejected["status"] == "rejected"
    assert "max_drawdown" in rejected["hard_gate_failures"]
    assert turnover_rejected["status"] == "rejected"
    assert "max_turnover_per_session" in turnover_rejected["hard_gate_failures"]
    assert better["report_only"]["max_feature_weight"]["binding"] is False


def test_primary_objective_binds_mark_to_market_equity():
    objective = normalize_objective_config()
    assert objective["primary"]["equity_basis"] == "mark_to_market"
    with pytest.raises(OptimizationError, match="equity_basis"):
        normalize_objective_config(
            {"primary": {"equity_basis": "liquidated"}}
        )
    with pytest.raises(OptimizationError, match="metrics.equity_basis"):
        score_portfolio_objective(
            {
                "equity_basis": "liquidated",
                "trade_count": 1,
                "total_return": 0.1,
                "max_drawdown": 0.0,
                "win_rate": 1.0,
                "profit_factor": 1.0,
                "turnover": 1.0,
                "trading_days": 1,
                "sharpe": 1.0,
            }
        )


def test_minimum_sample_gates_redact_unavailable_metrics_and_block_ranking():
    objective = {
        "hard_gates": {
            "min_observation_sessions": 3,
            "min_trade_count": 2,
            "max_drawdown": 1.0,
            "min_profit_factor": 0.0,
            "min_total_return": -1.0,
            "min_win_rate": 0.0,
            "max_turnover_per_session": 1_000_000.0,
        }
    }
    base = {
        "equity_basis": "mark_to_market",
        "trade_count": 2,
        "total_return": 0.12,
        "annualized_return": 0.30,
        "excess_return": 0.08,
        "max_drawdown": 0.10,
        "win_rate": 0.60,
        "profit_factor": 1.5,
        "turnover": 2.0,
        "trading_days": 3,
        "sharpe": 1.2,
        "sortino": 1.4,
        "start_date": "2024-01-01",
        "end_date": "2024-01-03",
    }

    measurable = score_portfolio_objective(base, objective)
    short_sessions = score_portfolio_objective(
        {**base, "trading_days": 2}, objective
    )
    short_trades = score_portfolio_objective(
        {**base, "trade_count": 1}, objective
    )

    assert measurable["status"] == "passed"
    assert measurable["score"] == pytest.approx(1.2)
    for unavailable, expected_gate in (
        (short_sessions, "min_observation_sessions"),
        (short_trades, "min_trade_count"),
    ):
        assert unavailable["status"] == "unavailable"
        assert unavailable["score"] is None
        assert unavailable["components"] == {}
        assert unavailable["primary_raw"] is None
        assert unavailable["normalized"]["annualized_return"] is None
        assert unavailable["normalized"]["sharpe"] is None
        assert unavailable["normalized"]["sortino"] is None
        assert unavailable["normalized"]["total_return"] == base["total_return"]
        assert unavailable["normalized"]["trading_days"] == (
            unavailable["sample_requirements"]["available_sessions"]
        )
        assert unavailable["normalized"]["trade_count"] == (
            unavailable["sample_requirements"]["available_trades"]
        )
        assert unavailable["date_range"] == {
            "start": "2024-01-01",
            "end": "2024-01-03",
        }
        assert expected_gate in unavailable["hard_gate_failures"]
        assert unavailable["unavailable_reason"]


def test_parameter_optimization_ranks_candidates_and_persists_evidence(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    _write_snapshot(snapshot_dir, "2024-01-01")
    _write_snapshot(snapshot_dir, "2024-01-02")
    database = tmp_path / "v3.sqlite3"
    payload = {
        "candidates": [
            {
                "model_config": _model_config("optimizer-low-risk"),
                "backtest_config": _backtest_config(0.20),
            },
            {
                "model_config": _model_config("optimizer-high-risk"),
                "backtest_config": _backtest_config(0.80),
            },
        ],
        "objective": {
            "hard_gates": {
                "min_trade_count": 1,
                "max_drawdown": 1.0,
                "min_profit_factor": 0.0,
                "min_total_return": -1.0,
                "min_win_rate": 0.0,
                "max_turnover_per_session": 1_000_000.0,
            }
        },
    }

    result = run_parameter_optimization(
        database,
        payload,
        snapshot_dir=snapshot_dir,
        bars=_bars(),
        output_dir=tmp_path / "optimization",
        report_dir=tmp_path / "reports",
    )

    assert result["status"] == "completed"
    assert result["candidate_count"] == 2
    assert result["eligible_candidate_count"] == 2
    assert result["best_candidate_id"] == result["best_candidate"]["candidate_id"]
    assert result["best_candidate"]["metrics"]["total_return"] > 0
    assert result["production_changed"] is False
    assert Path(result["artifact_path"]).is_file()

    with sqlite3.connect(str(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM optimization_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM optimization_candidates").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM backtest_evidence").fetchone()[0] == 2
