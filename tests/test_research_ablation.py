from __future__ import annotations

import pytest

from kinace_v3.research_ablation import (
    _capacity_audit,
    _exit_ablations,
    _factor_attribution,
)


def _bars() -> dict:
    return {
        "000001": [
            {"trade_date": "2026-01-01", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 1_000_000.0, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
            {"trade_date": "2026-01-02", "open": 10.0, "high": 10.8, "low": 9.9, "close": 10.5, "volume": 1_000_000.0, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
            {"trade_date": "2026-01-05", "open": 10.5, "high": 11.0, "low": 10.2, "close": 10.8, "volume": 1_000_000.0, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
            {"trade_date": "2026-01-06", "open": 10.8, "high": 11.2, "low": 10.5, "close": 11.0, "volume": 1_000_000.0, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
        ]
    }


def _scores() -> list[dict]:
    return [
        {"decision_date": "2026-01-01", "code": "000001", "score": 0.8},
        {"decision_date": "2026-01-02", "code": "000001", "score": 0.8},
        {"decision_date": "2026-01-05", "code": "000001", "score": 0.8},
    ]


def _config(cash: float = 100_000.0) -> dict:
    return {
        "initial_cash": cash,
        "max_positions": 1,
        "position_size_pct": 0.5,
        "lot_size": 100,
        "min_score": 0.5,
        "max_hold_sessions": 2,
        "stop_atr_multiple": 10.0,
        "target_atr_multiple": 10.0,
        "market_filter_enabled": False,
    }


def test_exit_ablation_returns_all_variants_and_delta_j():
    result = _exit_ablations(
        scores=_scores(),
        bars=_bars(),
        backtest_config=_config(),
        objective={"hard_gates": {"min_trade_count": 1, "max_drawdown": 1.0, "min_profit_factor": 0.0, "min_total_return": -1.0, "min_win_rate": 0.0}},
        random_seed=7,
    )

    assert result["base_label"] == "atr_and_time"
    assert {item["label"] for item in result["variants"]} == {
        "atr_and_time",
        "time_only",
        "atr_only",
        "no_active_exit",
        "random_signal_atr_time",
    }
    assert result["variants"][0]["delta_j_vs_base"] == 0.0


def test_capacity_audit_reports_execution_diagnostics():
    result = _capacity_audit(
        scores=_scores(),
        bars=_bars(),
        backtest_config=_config(),
        capacity_levels=[1_000_000.0, 10_000_000.0],
    )

    assert len(result["cases"]) == 2
    assert all("diagnostics" in item for item in result["cases"])
    assert result["cases"][0]["limit_up_blocked_rate"] == 0.0


def test_factor_attribution_allocates_trade_pnl_and_turnover():
    result = _factor_attribution(
        trades=[
            {
                "decision_date": "2026-01-01",
                "code": "000001",
                "pnl": 100.0,
                "entry_price": 10.0,
                "exit_price": 11.0,
                "shares": 100,
            }
        ],
        score_rows=[
            {
                "decision_date": "2026-01-01",
                "code": "000001",
                "components": {"gap": 0.6, "ret_5": 0.2},
            }
        ],
    )

    assert result["trades_attributed"] == 1
    assert result["factors"]["gap"]["pnl"] == pytest.approx(75.0)
    assert result["factors"]["ret_5"]["pnl"] == pytest.approx(25.0)
    assert result["factors"]["gap"]["share_of_turnover"] == pytest.approx(0.75)
