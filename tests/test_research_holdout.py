from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from kinace_v3.research_holdout import (
    DEFAULT_HOLDOUT_EXTENSION_POLICY,
    _first_trade_count_date,
    build_holdout_extension_freeze,
    holdout_extension_policy_id,
    normalize_holdout_extension_policy,
    objective_without_trade_count,
    load_holdout_extension_registry,
    register_holdout_extension_freeze,
    run_holdout_extension,
    write_holdout_extension_freeze,
)
from kinace_v3.feature_snapshot import FEATURE_NAMES
from kinace_v3.holdout_access import holdout_access_count, open_holdout_access


def _dates(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    return [(current + timedelta(days=index)).isoformat() for index in range(count)]


def _objective() -> dict:
    return {
        "hard_gates": {
            "min_trade_count": 30,
            "max_drawdown": 1.0,
            "min_profit_factor": 0.0,
            "min_total_return": -1.0,
            "min_win_rate": 0.0,
        }
    }


def _metrics(trade_count: int, trading_days: int) -> dict:
    return {
        "equity_basis": "mark_to_market",
        "total_return": 0.05,
        "max_drawdown": 0.05,
        "win_rate": 0.55,
        "turnover": 1.0,
        "sharpe": 1.5,
        "profit_factor": 1.4,
        "trade_count": trade_count,
        "trading_days": trading_days,
    }


def _trade(exit_date: str, index: int) -> dict:
    return {
        "code": f"000{index:03d}",
        "decision_date": exit_date,
        "entry_date": exit_date,
        "exit_date": exit_date,
    }


def _model_config() -> dict:
    return {
        "version": "holdout-extension-test",
        "mode": "research_holdout_test",
        "weights": {name: 0.125 for name in FEATURE_NAMES},
        "penalties": {
            "rsi_overbought": 80.0,
            "kdj_overbought": 100.0,
            "overheat_penalty": 1.0,
            "amplitude_threshold": 0.04,
            "amplitude_multiplier": 4.0,
            "amplitude_max_penalty": 0.35,
        },
    }


def test_holdout_extension_policy_identity_is_frozen():
    policy = normalize_holdout_extension_policy({})

    assert policy == DEFAULT_HOLDOUT_EXTENSION_POLICY
    assert holdout_extension_policy_id(policy) == (
        "7e4e5d72a1b91a721ec4f8dd5daff3f0ca1bf7dfa5d22ee99cdf9b12c984aacf"
    )


def test_first_trade_count_date_uses_completed_exit_dates():
    candidate_dates = _dates("2026-01-01", 5)
    trades = [_trade(candidate_dates[2], index) for index in range(2)]
    trades.append(_trade(candidate_dates[4], 99))

    assert _first_trade_count_date(trades, candidate_dates, 3) == candidate_dates[4]


def test_objective_without_trade_count_sets_zero_threshold():
    objective = _objective()

    adjusted = objective_without_trade_count(objective)

    assert adjusted["hard_gates"]["min_trade_count"] == 0
    assert objective["hard_gates"]["min_trade_count"] == 30


def test_run_holdout_extension_stops_at_first_30_trade_session(tmp_path, monkeypatch):
    dates = _dates("2026-01-01", 70)
    initial_end = dates[59]
    stop_date = dates[64]
    max_end = dates[69]

    def _discover(snapshot_dir, *, start_date=None, end_date=None):
        selected = [
            {"decision_date": value, "path": str(tmp_path / f"{value}.json")}
            for value in dates
        ]
        if start_date is not None:
            selected = [item for item in selected if item["decision_date"] >= start_date]
        if end_date is not None:
            selected = [item for item in selected if item["decision_date"] <= end_date]
        return selected

    calls: list[str] = []

    def _runner(database, candidate, snapshot_dir, *, start_date, end_date, **kwargs):
        calls.append(end_date)
        if end_date == max_end:
            trades = [_trade(dates[60], index) for index in range(29)]
            trades.append(_trade(stop_date, 30))
        else:
            trades = [_trade(dates[60], index) for index in range(29)]
            trades.append(_trade(stop_date, 30))
        return {
            "run_id": f"run-{end_date}",
            "output_path": kwargs.get("output_path"),
            "date_range": {"start": start_date, "end": end_date},
            "metrics": _metrics(
                len(trades),
                len([d for d in dates if start_date <= d <= end_date]),
            ),
            "trades": trades,
        }

    monkeypatch.setattr("kinace_v3.research_holdout.discover_snapshots", _discover)
    monkeypatch.setattr("kinace_v3.research_holdout.load_market_bars", lambda _: {})
    objective = _objective()
    access_path = tmp_path / "holdout-access-registry.json"
    access = open_holdout_access(
        "candidate-test",
        "protocol-hash",
        {"start": dates[0], "end": initial_end, "sessions": 60},
        eligible=True,
        registry_path=access_path,
    )
    freeze = build_holdout_extension_freeze(
        loop_id="loop-test",
        candidate_signature="candidate-test",
        protocol_candidate_id="protocol-candidate-test",
        evaluation_protocol_hash="protocol-hash",
        model_config=_model_config(),
        backtest_config={"initial_cash": 100000.0},
        objective=objective,
        initial_holdout={
            "start": dates[0],
            "end": initial_end,
            "sessions": 60,
        },
        initial_metrics=_metrics(21, 60),
        initial_objective={"status": "passed", "hard_gate_failures": []},
        policy={},
        holdout_access_id=access["holdout_access_id"],
        holdout_access_registry_path=access_path,
    )
    freeze_path = tmp_path / "freeze.json"
    write_holdout_extension_freeze(freeze, freeze_path)
    registry_path = tmp_path / "registry.json"
    register_holdout_extension_freeze(
        freeze,
        database=tmp_path / "research.sqlite3",
        freeze_path=freeze_path,
        registry_path=registry_path,
    )

    result = run_holdout_extension(
        tmp_path / "research.sqlite3",
        freeze_path,
        snapshot_dir=tmp_path / "snapshots",
        output_dir=tmp_path / "outputs",
        report_dir=tmp_path / "reports",
        registry_path=registry_path,
        holdout_access_registry_path=access_path,
        portfolio_runner=_runner,
    )

    assert result["status"] == "passed"
    assert result["extension"]["end"] == stop_date
    assert result["extension"]["end_reason"] == "trade_count_reached"
    assert result["metrics"]["trade_count"] == 30
    assert calls == [max_end, stop_date]
    assert Path(result["artifact_path"]).is_file()
    assert Path(result["markdown_path"]).is_file()
    registry = load_holdout_extension_registry(registry_path)
    assert registry["records"][0]["status"] == "passed"
    assert registry["records"][0]["last_extension_id"] == result["extension_id"]
    assert result["holdout_access_id"] == access["holdout_access_id"]
    assert result["holdout_access_read_count"] == 1
    assert holdout_access_count(access_path) == 1
