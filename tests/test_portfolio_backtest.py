from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kinace_v3.data_collection.ingest import ingest_records
from kinace_v3.data_collection.models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    RevisionStatus,
    SecurityStateRecord,
)
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.feature_snapshot import FEATURE_NAMES, create_feature_snapshot, write_feature_snapshot
from kinace_v3.portfolio_backtest import (
    load_market_bar_views,
    load_market_bars,
    run_portfolio_backtest,
)


def _model_config(version: str = "portfolio-test") -> dict:
    return {
        "version": version,
        "mode": "continuous_test",
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
            model_config=_model_config(),
            data_gate={"status": "synthetic", "ready": True},
        ),
        snapshot_dir / f"{decision_date}.json",
    )


def _bar(date: str, price: float) -> dict:
    return {
        "trade_date": date,
        "open": price,
        "high": price + 0.20,
        "low": price - 0.20,
        "close": price + 0.05,
        "volume": 1_000_000.0,
        "is_suspended": False,
        "is_limit_up": False,
        "is_limit_down": False,
    }


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
        result[code] = [
            _bar(date, 10.0 + code_offset + index * 0.03)
            for index, date in enumerate(dates)
        ]
    return result


def test_portfolio_backtest_runs_one_continuous_account_and_persists_evidence(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    _write_snapshot(snapshot_dir, "2024-01-01")
    _write_snapshot(snapshot_dir, "2024-01-02")
    database = tmp_path / "v3.sqlite3"
    output = tmp_path / "portfolio.json"

    result = run_portfolio_backtest(
        database,
        _model_config(),
        snapshot_dir,
        bars=_bars(),
        config={
            "initial_cash": 100_000.0,
            "max_positions": 2,
            "position_size_pct": 0.5,
            "min_score": 0.0,
            "max_hold_sessions": 3,
            "stop_atr_multiple": 10.0,
            "target_atr_multiple": 10.0,
        },
        output_path=output,
    )

    assert result["mode"] == "continuous_portfolio"
    assert result["continuous"] is True
    assert result["snapshot_manifest"]["snapshot_count"] == 2
    assert result["score_count"] == 4
    assert result["metrics"]["trade_count"] >= 2
    assert result["metrics"]["trading_days"] >= 7
    assert output.is_file()
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["run_id"] == result["run_id"]
    assert saved["metrics"]["final_equity"] == result["metrics"]["final_equity"]
    assert result["equity_basis"] == "mark_to_market"
    assert result["equity"] == result["mark_to_market_equity"]
    assert saved["mark_to_market_equity"] == result["mark_to_market_equity"]
    assert saved["liquidated_equity"] == result["liquidated_equity"]
    assert saved["closed_trade_stats"] == result["closed_trade_stats"]
    assert saved["open_positions"] == result["open_positions"]
    assert result["benchmark_proxy"]["label"] == "equal-weight market proxy"
    assert result["benchmark_proxy"]["is_official_index"] is False
    assert (
        result["benchmark_proxy"]["return_anomaly_policy"]
        == result["metrics"]["benchmark_anomaly_policy"]
    )
    assert saved["benchmark_proxy"] == result["benchmark_proxy"]

    with sqlite3.connect(str(database)) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "score_replays",
                "experiments",
                "backtest_runs",
                "backtest_trades",
                "backtest_evidence",
            )
        }
    assert counts["score_replays"] == 1
    assert counts["experiments"] == 1
    assert counts["backtest_runs"] == 1
    assert counts["backtest_trades"] == result["metrics"]["trade_count"]
    assert counts["backtest_evidence"] == 1


def test_portfolio_backtest_is_deterministic_for_same_inputs(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    _write_snapshot(snapshot_dir, "2024-01-01")
    _write_snapshot(snapshot_dir, "2024-01-02")
    database = tmp_path / "v3.sqlite3"
    kwargs = {
        "bars": _bars(),
        "config": {
            "initial_cash": 100_000.0,
            "max_positions": 2,
            "position_size_pct": 0.5,
            "min_score": 0.0,
            "max_hold_sessions": 3,
            "stop_atr_multiple": 10.0,
            "target_atr_multiple": 10.0,
        },
    }

    first = run_portfolio_backtest(
        database,
        _model_config(),
        snapshot_dir,
        output_path=tmp_path / "first.json",
        **kwargs,
    )
    second = run_portfolio_backtest(
        database,
        _model_config(),
        snapshot_dir,
        output_path=tmp_path / "second.json",
        **kwargs,
    )

    assert first["run_id"] == second["run_id"]
    assert first["metrics"] == second["metrics"]
    assert first["trades"] == second["trades"]
    with sqlite3.connect(str(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM backtest_evidence").fetchone()[0] == 1


def test_portfolio_backtest_honors_requested_end_date(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    _write_snapshot(snapshot_dir, "2024-01-01")
    _write_snapshot(snapshot_dir, "2024-01-02")

    result = run_portfolio_backtest(
        tmp_path / "v3.sqlite3",
        _model_config(),
        snapshot_dir,
        start_date="2024-01-01",
        end_date="2024-01-04",
        bars=_bars(),
        config={
            "initial_cash": 100_000.0,
            "max_positions": 2,
            "position_size_pct": 0.5,
            "min_score": 0.0,
            "max_hold_sessions": 10,
            "stop_atr_multiple": 10.0,
            "target_atr_multiple": 10.0,
        },
        output_path=tmp_path / "bounded.json",
    )

    assert result["date_range"]["end"] == "2024-01-04"
    assert result["equity"][-1]["trade_date"] == "2024-01-04"
    assert all(trade["exit_date"] <= "2024-01-04" for trade in result["trades"])


def test_extending_end_date_preserves_completed_raw_trade_prices(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    _write_snapshot(snapshot_dir, "2024-01-01")
    _write_snapshot(snapshot_dir, "2024-01-02")
    config = {
        "initial_cash": 100_000.0,
        "max_positions": 2,
        "position_size_pct": 0.5,
        "min_score": 0.0,
        "max_hold_sessions": 1,
        "stop_atr_multiple": 10.0,
        "target_atr_multiple": 10.0,
    }

    bounded = run_portfolio_backtest(
        tmp_path / "bounded.sqlite3",
        _model_config(),
        snapshot_dir,
        end_date="2024-01-04",
        bars=_bars(),
        config=config,
        output_path=tmp_path / "bounded.json",
    )
    extended = run_portfolio_backtest(
        tmp_path / "extended.sqlite3",
        _model_config(),
        snapshot_dir,
        end_date="2024-01-10",
        bars=_bars(),
        config=config,
        output_path=tmp_path / "extended.json",
    )
    bounded_completed = {
        (trade["code"], trade["entry_date"], trade["exit_date"]): trade
        for trade in bounded["trades"]
        if trade["exit_date"] <= "2024-01-04"
    }
    extended_completed = {
        (trade["code"], trade["entry_date"], trade["exit_date"]): trade
        for trade in extended["trades"]
        if trade["exit_date"] <= "2024-01-04"
    }

    assert bounded_completed
    assert extended_completed == bounded_completed


def test_load_market_bars_streams_dataset_rows(tmp_path, monkeypatch):
    database = tmp_path / "v3.sqlite3"
    store = RevisionStore(database)
    ingest_records(
        store,
        "daily_bars",
        [
            DailyBarRecord(
                ts_code="000001",
                trade_date="2024-01-02",
                open=10.0,
                high=10.5,
                low=9.8,
                close=10.2,
                vol=1000.0,
                amount=10_200.0,
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(
        store,
        "security_state",
        [
            SecurityStateRecord(
                ts_code="000001",
                trade_date="2024-01-02",
                is_suspended=False,
                is_limit_up=False,
                is_limit_down=False,
                prev_close=10.0,
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )

    def _unexpected_current_records(*args, **kwargs):
        raise AssertionError("market bar loading must not materialize current_records")

    monkeypatch.setattr(RevisionStore, "current_records", _unexpected_current_records)
    bars = load_market_bars(database)

    assert list(bars) == ["000001"]
    assert bars["000001"][0]["trade_date"] == "2024-01-02"
    assert bars["000001"][0]["is_suspended"] is False


def test_load_market_bars_returns_explicit_raw_or_adjusted_views(tmp_path):
    database = tmp_path / "v3.sqlite3"
    store = RevisionStore(database)
    bars = [
        DailyBarRecord(
            ts_code="000001",
            trade_date="2024-01-02",
            open=99.0,
            high=101.0,
            low=98.0,
            close=100.0,
            pre_close=99.0,
            vol=1000.0,
            amount=100_000.0,
        ),
        DailyBarRecord(
            ts_code="000001",
            trade_date="2024-01-03",
            open=49.0,
            high=51.0,
            low=48.0,
            close=50.0,
            pre_close=100.0,
            vol=2000.0,
            amount=100_000.0,
        ),
    ]
    states = [
        SecurityStateRecord(
            ts_code="000001",
            trade_date=bar.trade_date,
            is_suspended=False,
            is_limit_up=False,
            is_limit_down=False,
            prev_close=bar.pre_close,
        )
        for bar in bars
    ]
    factors = [
        AdjustmentFactorRecord(
            ts_code="000001",
            trade_date="2024-01-02",
            adj_factor=1.0,
            adjustment_basis_date="2024-01-03",
        ),
        AdjustmentFactorRecord(
            ts_code="000001",
            trade_date="2024-01-03",
            adj_factor=2.0,
            adjustment_basis_date="2024-01-03",
        ),
    ]
    ingest_records(
        store,
        "daily_bars",
        bars,
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(
        store,
        "security_state",
        states,
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(
        store,
        "adjustment_factors",
        factors,
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )

    raw = load_market_bars(database, price_basis="raw")
    adjusted = load_market_bars(database, price_basis="total_return")
    views = load_market_bar_views(database, research_price_basis="total_return")

    assert [row["close"] for row in raw["000001"]] == [100.0, 50.0]
    assert [row["close"] for row in adjusted["000001"]] == [100.0, 100.0]
    assert views["execution"]["price_basis"] == "raw"
    assert views["research"]["price_basis"] == "total_return"
