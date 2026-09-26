from __future__ import annotations

from pathlib import Path

from kinace_v3.research_diagnose import (
    _markdown_report,
    build_diagnosis,
    build_market_proxy,
)


def _equity(values: list[float]) -> list[dict]:
    dates = ("2026-01-02", "2026-01-05", "2026-01-06")
    return [
        {
            "trade_date": trade_date,
            "total_assets": value,
            "positions": 1,
        }
        for trade_date, value in zip(dates, values)
    ]


def _trade(
    code: str,
    score: float,
    pnl: float,
    exit_reason: str,
) -> dict:
    return {
        "code": code,
        "decision_date": "2026-01-02",
        "entry_date": "2026-01-02",
        "exit_date": "2026-01-05",
        "pnl": pnl,
        "return_pct": pnl / 100_000.0,
        "fees": 10.0,
        "hold_sessions": 2,
        "exit_reason": exit_reason,
        "metadata": {"score": score},
    }


def _candidate_report() -> dict:
    return {
        "run_id": "candidate-run",
        "date_range": {"start": "2026-01-02", "end": "2026-01-06"},
        "snapshot_manifest": {"snapshot_set_id": "snapshot-set"},
        "config": {
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "transfer_fee_rate": 0.00001,
            "slippage_bps": 5.0,
        },
        "initial_cash": 100_000.0,
        "equity": _equity([100_000.0, 90_000.0, 85_000.0]),
        "trades": [
            _trade("000001", 0.80, -8_000.0, "stop"),
            _trade("000002", 0.60, -7_000.0, "time_stop"),
        ],
        "metrics": {
            "equity_basis": "mark_to_market",
            "total_return": -0.15,
            "max_drawdown": 0.15,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "turnover": 0.0,
            "trade_count": 2,
        },
    }


def _baseline_report() -> dict:
    return {
        "run_id": "baseline-run",
        "date_range": {"start": "2026-01-02", "end": "2026-01-06"},
        "snapshot_manifest": {"snapshot_set_id": "snapshot-set"},
        "config": {
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "transfer_fee_rate": 0.00001,
            "slippage_bps": 5.0,
        },
        "initial_cash": 100_000.0,
        "equity": _equity([100_000.0, 98_000.0, 97_000.0]),
        "trades": [],
        "metrics": {
            "equity_basis": "mark_to_market",
            "total_return": -0.03,
            "max_drawdown": 0.03,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "turnover": 0.0,
            "trade_count": 0,
        },
    }


def _bars() -> dict[str, list[dict]]:
    dates = ("2026-01-02", "2026-01-05", "2026-01-06")
    result = {}
    for code_offset, code in enumerate(("000001", "000002")):
        result[code] = []
        for index, trade_date in enumerate(dates):
            price = 10.0 - index * 0.2 + code_offset
            result[code].append(
                {
                    "trade_date": trade_date,
                    "open": price,
                    "high": price + 0.1,
                    "low": price - 0.1,
                    "close": price,
                    "volume": 1_000_000.0,
                    "is_limit_up": index == 0 and code == "000001",
                    "is_limit_down": index == 2 and code == "000001",
                    "is_suspended": False,
                }
            )
    return result


def test_market_proxy_tracks_returns_and_limit_state():
    rows = build_market_proxy(
        _bars(),
        start_date="2026-01-02",
        end_date="2026-01-06",
    )

    assert len(rows) == 3
    assert rows[0]["limit_up_count"] == 1
    assert rows[-1]["limit_down_count"] == 1
    assert all("benchmark_return" in row for row in rows)


def test_market_proxy_retains_raw_outlier_and_audits_sanitized_curve():
    bars = {
        "000001": [
            {"trade_date": "2026-01-02", "close": 1.0},
            {"trade_date": "2026-01-05", "close": 2.0},
        ],
        "000002": [
            {"trade_date": "2026-01-02", "close": 1.0},
            {"trade_date": "2026-01-05", "close": 1.0},
        ],
    }

    rows = build_market_proxy(
        bars,
        start_date="2026-01-02",
        end_date="2026-01-05",
    )

    assert rows[-1]["benchmark_return"] == 0.5
    assert rows[-1]["benchmark_sanitized_return"] == 0.0
    assert rows[-1]["benchmark_exclusions"] == [
        {
            "date": "2026-01-05",
            "code": "000001",
            "raw_return": 1.0,
            "exclusion_reason": "absolute_return_gt_0.50",
        }
    ]


def test_diagnosis_attributes_loss_to_stock_industry_exit_and_score():
    diagnosis = build_diagnosis(
        candidate_report=_candidate_report(),
        baseline_report=_baseline_report(),
        bars=_bars(),
        industry_by_code={"000001": "software", "000002": "hardware"},
        candidate_signature="candidate-signature",
        loop_id="loop-id",
        code_version="diagnosis-test",
        objective={
            "hard_gates": {
                "min_trade_count": 1,
                "max_drawdown": 1.0,
                "min_profit_factor": 0.0,
                "min_total_return": -1.0,
                "min_win_rate": 0.0,
            }
        },
    )

    assert diagnosis["candidate_metrics"]["total_return"] == -0.15
    assert diagnosis["baseline_metrics"]["total_return"] == -0.03
    assert diagnosis["by_stock"][0]["key"] == "000001"
    assert {row["key"] for row in diagnosis["by_industry"]} == {"software", "hardware"}
    assert {row["key"] for row in diagnosis["by_exit_reason"]} == {"stop", "time_stop"}
    assert diagnosis["by_score_bucket"][0]["key"] == "06:>=0.80"
    assert diagnosis["worst_days"][0]["trade_date"] == "2026-01-05"
    assert diagnosis["findings"]
    comparison = diagnosis["same_window_comparison"]
    assert comparison["same_window"] is True
    assert comparison["same_snapshot_set"] is True
    assert comparison["cost_fields_equal"] is True
    assert comparison["verdict"] == "challenger_not_qualified"
    assert comparison["trade_overlap"]["union_trades"] == 2
    assert diagnosis["market_proxy_label"] == "equal-weight market proxy"
    assert diagnosis["market_proxy_is_official_index"] is False
    report = _markdown_report(diagnosis)
    assert "equal-weight market proxy" in report
    assert "not an official index" in report
