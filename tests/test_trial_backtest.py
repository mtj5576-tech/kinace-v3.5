from __future__ import annotations

import builtins
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from kinace_v3.experiments import record_experiment, record_replay
from kinace_v3.feature_snapshot import (
    FEATURE_NAMES,
    create_feature_snapshot,
    write_feature_snapshot,
)
from kinace_v3.score_replay import replay_candidate
from kinace_v3.trial_backtest import (
    TrialBacktestError,
    normalize_market_bars,
    normalize_market_views,
    run_trial_backtest,
    simulate_trial,
)


def _model_config(version: str = "trial-backtest-test") -> dict:
    return {
        "version": version,
        "mode": "synthetic_test",
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


def _replay_fixture(
    tmp_path: Path,
    *,
    name: str,
    codes: tuple[str, ...],
    decision_date: str = "2024-01-02",
) -> dict:
    candidates = []
    for index, code in enumerate(codes):
        value = float(index + 1)
        candidates.append(
            {
                "code": code,
                "raw_inputs": {
                    feature: value + feature_index / 10.0
                    for feature_index, feature in enumerate(FEATURE_NAMES)
                },
                "penalty_inputs": {
                    "rsi": 55.0,
                    "kdj_j": 70.0,
                    "amplitude_5d": 0.02,
                },
                "labels": {"forward_return": 0.01},
                "point_in_time": {"as_of_utc": "2024-01-02T07:00:00Z"},
            }
        )
    snapshot = write_feature_snapshot(
        create_feature_snapshot(
            decision_date=decision_date,
            candidates=candidates,
            model_config=_model_config(name),
            data_gate={"status": "synthetic", "ready": False},
        ),
        tmp_path / f"{name}-snapshot.json",
    )
    candidate = _model_config(f"{name}-candidate")
    replay = replay_candidate(
        snapshot,
        candidate,
        tmp_path / f"{name}-replay.json",
    )
    database = tmp_path / f"{name}.sqlite3"
    replay_id = record_replay(database, replay, code_version="trial-backtest-test")
    experiment_id = record_experiment(
        database,
        code_version="trial-backtest-test",
        data_snapshot_hash=snapshot["artifact_sha256"],
        search_space={"name": [name]},
        seed=7,
        objective_config={"metric": "final_equity", "direction": "maximize"},
        candidate_id=replay["payload"]["candidate_id"],
        replay_id=replay_id,
        artifact_paths={
            "snapshot": snapshot["artifact_path"],
            "replay": replay["artifact_path"],
        },
    )
    return {
        "database": database,
        "snapshot": snapshot,
        "replay": replay,
        "replay_id": replay_id,
        "candidate_id": replay["payload"]["candidate_id"],
        "experiment_id": experiment_id,
    }


def _bar(
    trade_date: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
    *,
    volume: float = 1000.0,
    is_suspended: bool = False,
    is_limit_up: bool = False,
    is_limit_down: bool = False,
) -> dict:
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


def _single_code_bars() -> dict[str, list[dict]]:
    return {
        "000001": [
            _bar("2024-01-01", 90.0, 91.0, 89.0, 90.0),
            _bar("2024-01-02", 95.0, 106.0, 94.0, 105.0),
            _bar("2024-01-03", 101.0, 108.0, 100.0, 104.0),
            _bar("2024-01-04", 104.0, 106.0, 102.0, 105.0),
            _bar("2024-01-05", 105.0, 108.0, 104.0, 107.0),
        ]
    }


def test_deterministic_equity_is_persisted_once_for_the_exact_lineage(tmp_path):
    fixture = _replay_fixture(tmp_path, name="deterministic", codes=("000001",))
    bars = _single_code_bars()
    config = {
        "initial_cash": 100_000.0,
        "position_size_pct": 0.5,
        "lot_size": 1,
        "max_hold_sessions": 2,
        "slippage_bps": 0.0,
    }

    first = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        bars,
        config=config,
    )
    reversed_bars = {
        code: list(reversed(code_bars)) for code, code_bars in bars.items()
    }
    second = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        reversed_bars,
        config=config,
    )

    assert first["run_id"] == second["run_id"]
    assert first["equity"] == second["equity"]
    assert first["metrics"] == second["metrics"]
    assert first["trades"] == second["trades"]
    assert len(first["equity"]) >= 3
    assert first["metrics"]["holding_periods"]["trade_count"] == 1
    assert first["trades"][0]["entry_date"] > first["trades"][0]["decision_date"]

    with sqlite3.connect(str(fixture["database"])) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT * FROM backtest_runs WHERE run_id=?", (first["run_id"],)
        ).fetchone()
        trade = connection.execute(
            "SELECT * FROM backtest_trades WHERE run_id=?", (first["run_id"],)
        ).fetchone()
        counts = connection.execute(
            """
            SELECT COUNT(*) AS runs,
                   (SELECT COUNT(*) FROM backtest_trades) AS trades
            FROM backtest_runs
            """
        ).fetchone()

    assert counts["runs"] == 1
    assert counts["trades"] == 1
    assert run["experiment_id"] == fixture["experiment_id"]
    assert run["candidate_id"] == fixture["candidate_id"]
    assert run["replay_id"] == fixture["replay_id"]
    assert run["config_hash"] == first["config_hash"]
    assert trade["experiment_id"] == fixture["experiment_id"]
    assert trade["candidate_id"] == fixture["candidate_id"]
    assert trade["replay_id"] == fixture["replay_id"]
    assert json.loads(run["equity_json"]) == first["equity"]


def test_fees_and_slippage_reduce_deterministic_equity(tmp_path):
    fixture = _replay_fixture(tmp_path, name="costs", codes=("000001",))
    bars = {
        "000001": [
            _bar("2024-01-01", 90.0, 91.0, 89.0, 90.0),
            _bar("2024-01-02", 95.0, 106.0, 94.0, 100.0),
            _bar("2024-01-03", 100.0, 101.0, 99.0, 100.0),
            _bar("2024-01-04", 100.0, 101.0, 99.0, 100.0),
        ]
    }
    zero_cost = {
        "initial_cash": 100_000.0,
        "position_size_pct": 1.0,
        "lot_size": 1,
        "max_hold_sessions": 1,
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "stamp_duty_rate": 0.0,
        "transfer_fee_rate": 0.0,
        "slippage_bps": 0.0,
        "max_volume_participation": 1.0,
        "stop_atr_multiple": 100.0,
        "target_atr_multiple": 100.0,
    }
    with_costs = {
        **zero_cost,
        "commission_rate": 0.001,
        "min_commission": 2.0,
        "stamp_duty_rate": 0.001,
        "transfer_fee_rate": 0.0001,
        "slippage_bps": 100.0,
    }

    baseline = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        bars,
        config=zero_cost,
    )
    charged = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        bars,
        config=with_costs,
    )

    baseline_trade = baseline["trades"][0]
    charged_trade = charged["trades"][0]
    assert baseline_trade["fees"] == 0.0
    assert baseline_trade["slippage"] == 0.0
    assert charged["metrics"]["final_equity"] < baseline["metrics"]["final_equity"]
    assert charged_trade["fees"] == pytest.approx(315.491, abs=0.001)
    assert charged_trade["slippage"] == pytest.approx(1978.0, abs=0.001)
    assert charged["metrics"]["total_fees"] == pytest.approx(315.491, abs=0.001)
    assert charged["metrics"]["total_slippage"] == pytest.approx(1978.0, abs=0.001)


def test_volume_participation_caps_entry_shares():
    result = simulate_trial(
        scores=[{"decision_date": "2024-01-02", "code": "000001", "score": 1.0}],
        bars={
            "000001": [
                _bar("2024-01-01", 90.0, 91.0, 89.0, 90.0, volume=1_000.0),
                _bar("2024-01-02", 95.0, 100.0, 94.0, 99.0, volume=1_000.0),
                _bar("2024-01-03", 100.0, 102.0, 98.0, 101.0, volume=100.0),
                _bar("2024-01-04", 101.0, 103.0, 99.0, 102.0, volume=100.0),
            ]
        },
        config={
            "initial_cash": 100_000.0,
            "position_size_pct": 1.0,
            "lot_size": 1,
            "max_hold_sessions": 1,
            "max_volume_participation": 0.5,
            "stop_atr_multiple": 10.0,
            "target_atr_multiple": 10.0,
        },
    )

    assert result["trades"][0]["shares"] == 50


def test_market_filter_blocks_entries_in_declining_market():
    bars = {
        "000001": [
            _bar("2024-01-01", 10.0, 10.1, 9.9, 10.0),
            _bar("2024-01-02", 10.0, 10.1, 9.9, 10.0),
            _bar("2024-01-03", 9.0, 9.1, 8.9, 9.0),
            _bar("2024-01-04", 8.0, 8.1, 7.9, 8.0),
        ]
    }
    scores = [{"decision_date": "2024-01-02", "code": "000001", "score": 1.0}]
    config = {
        "initial_cash": 100_000.0,
        "position_size_pct": 1.0,
        "lot_size": 1,
        "max_hold_sessions": 1,
        "stop_atr_multiple": 10.0,
        "target_atr_multiple": 10.0,
        "market_filter_enabled": True,
        "market_filter_sessions": 1,
        "min_market_return": 0.0,
    }

    filtered = simulate_trial(scores=scores, bars=bars, config=config)
    unfiltered = simulate_trial(
        scores=scores,
        bars=bars,
        config={**config, "market_filter_enabled": False},
    )

    assert filtered["trades"] == []
    assert filtered["metrics"]["trade_count"] == 0
    assert [row["trade_date"] for row in filtered["equity"]] == [
        row["trade_date"] for row in unfiltered["equity"]
    ]
    assert unfiltered["metrics"]["trade_count"] == 1


def test_normalize_market_bars_is_order_independent():
    first = {"code": "000001", **_bar("2024-01-02", 10.0, 10.5, 9.5, 10.2)}
    second = {"code": "000001", **_bar("2024-01-01", 9.5, 10.1, 9.4, 10.0)}
    third = {"code": "000001", **_bar("2024-01-03", 10.2, 10.8, 10.0, 10.6)}
    other = {"code": "000002", **_bar("2024-01-02", 20.0, 20.5, 19.5, 20.2)}

    expected = normalize_market_bars({"000001": [second, first, third], "000002": [other]})
    shuffled = normalize_market_bars({"000002": [other], "000001": [third, second, first]})
    reversed_rows = normalize_market_bars(
        {"000001": [third, first, second][::-1], "000002": [other]}
    )

    assert shuffled == expected
    assert reversed_rows == expected
    assert list(expected) == ["000001", "000002"]
    assert [row["trade_date"] for row in expected["000001"]] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]


def test_normalize_market_bars_validates_every_row_past_a_valid_first_row():
    first = {"code": "000001", **_bar("2024-01-01", 10.0, 10.5, 9.5, 10.2)}
    second = {"code": "000001", **_bar("2024-01-02", 10.2, 10.7, 10.0, 10.5)}
    missing_status = dict(second)
    missing_status.pop("is_limit_down")
    cases = [
        ([first, first], "duplicate market bar"),
        ([first, {**second, "volume": -1.0}], "bar.volume cannot be negative"),
        ([first, {**second, "volume": float("nan")}], "bar.volume must be a finite number"),
        ([first, {**second, "high": 10.1}], "bar high is inconsistent"),
        ([first, missing_status], "bar.is_limit_down must be a boolean"),
        ([first, {**second, "is_suspended": 0}], "bar.is_suspended must be a boolean"),
        ([first, {**second, "trade_date": "20240102"}], "bar.trade_date is not an ISO date"),
    ]

    for rows, message in cases:
        with pytest.raises(TrialBacktestError, match=message):
            normalize_market_bars({"000001": rows})


def test_normalize_market_bars_requires_code_when_not_supplied_by_mapping():
    with pytest.raises(TrialBacktestError, match="bar.code must be a non-empty string"):
        normalize_market_bars([_bar("2024-01-01", 10.0, 10.5, 9.5, 10.2)])


def test_run_trial_backtest_rejects_duplicate_later_normalized_row(tmp_path):
    fixture = _replay_fixture(tmp_path, name="late-duplicate", codes=("000001",))
    first = {"code": "000001", **_bar("2024-01-01", 90.0, 91.0, 89.0, 90.0)}

    with pytest.raises(TrialBacktestError, match="duplicate market bar"):
        run_trial_backtest(
            fixture["database"],
            fixture["replay_id"],
            fixture["candidate_id"],
            {"000001": [first, dict(first)]},
        )


def test_simulate_trial_computes_minimum_decision_date_once(monkeypatch):
    original_min = builtins.min
    decision_date_min_calls = 0

    def _counting_min(*args, **kwargs):
        nonlocal decision_date_min_calls
        if (
            len(args) == 1
            and isinstance(args[0], list)
            and args[0]
            and all(isinstance(value, str) for value in args[0])
        ):
            decision_date_min_calls += 1
        return original_min(*args, **kwargs)

    monkeypatch.setattr(builtins, "min", _counting_min)
    scores = [
        {"decision_date": "2024-01-02", "code": "000001", "score": 1.0},
        {"decision_date": "2024-01-03", "code": "000001", "score": 1.0},
    ]
    bars = {
        "000001": [
            _bar("2024-01-01", 90.0, 91.0, 89.0, 90.0),
            _bar("2024-01-02", 95.0, 100.0, 94.0, 99.0),
            _bar("2024-01-03", 100.0, 102.0, 98.0, 101.0),
            _bar("2024-01-04", 101.0, 103.0, 99.0, 102.0),
            _bar("2024-01-05", 102.0, 104.0, 100.0, 103.0),
        ]
    }

    simulate_trial(
        scores=scores,
        bars=bars,
        config={
            "initial_cash": 100_000.0,
            "position_size_pct": 1.0,
            "lot_size": 1,
            "max_hold_sessions": 1,
            "max_volume_participation": 1.0,
            "stop_atr_multiple": 10.0,
            "target_atr_multiple": 10.0,
        },
    )

    assert decision_date_min_calls == 1


def test_next_session_entry_and_t_plus_one_exit_do_not_use_decision_bar(tmp_path):
    fixture = _replay_fixture(tmp_path, name="timing", codes=("000001",))
    bars = {
        "000001": [
            _bar("2024-01-01", 90.0, 91.0, 89.0, 90.0),
            _bar("2024-01-02", 95.0, 106.0, 94.0, 105.0),
            _bar("2024-01-03", 100.0, 102.0, 1.0, 101.0),
            _bar("2024-01-04", 99.0, 100.0, 98.0, 99.0),
        ]
    }

    result = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        bars,
        config={
            "initial_cash": 100_000.0,
            "position_size_pct": 0.5,
            "lot_size": 1,
            "max_hold_sessions": 1,
            "slippage_bps": 0.0,
        },
    )

    trade = result["trades"][0]
    assert trade["decision_date"] == "2024-01-02"
    assert trade["entry_date"] == "2024-01-03"
    assert trade["intended_entry"] == 100.0
    assert trade["entry_price"] == 100.0
    assert trade["exit_date"] == "2024-01-04"
    assert trade["exit_reason"] == "time_stop"


def test_missing_replay_and_experiment_lineage_is_rejected(tmp_path):
    fixture = _replay_fixture(tmp_path, name="missing", codes=("000001",))
    bars = _single_code_bars()

    with pytest.raises(TrialBacktestError, match="candidate_id does not match"):
        run_trial_backtest(
            fixture["database"],
            fixture["replay_id"],
            "unrelated-candidate",
            bars,
        )

    with sqlite3.connect(str(fixture["database"])) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM experiments")
        connection.execute("DELETE FROM score_replays")

    with pytest.raises(TrialBacktestError, match="replay lineage is not recorded"):
        run_trial_backtest(
            fixture["database"],
            fixture["replay_id"],
            fixture["candidate_id"],
            bars,
        )

    record_replay(
        fixture["database"],
        fixture["replay"],
        code_version="trial-backtest-test",
    )
    with pytest.raises(TrialBacktestError, match="experiment lineage is not recorded"):
        run_trial_backtest(
            fixture["database"],
            fixture["replay_id"],
            fixture["candidate_id"],
            bars,
        )


def test_position_limit_caps_entries_and_persists_exact_lineage(tmp_path):
    fixture = _replay_fixture(
        tmp_path,
        name="limits",
        codes=("000001", "000002", "000003"),
    )
    bars = {
        code: deepcopy(_single_code_bars()["000001"])
        for code in ("000001", "000002", "000003")
    }
    result = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        bars,
        config={
            "initial_cash": 300_000.0,
            "max_positions": 2,
            "position_size_pct": 0.3,
            "lot_size": 1,
            "max_hold_sessions": 2,
        },
    )

    assert len(result["trades"]) == 2
    assert max(point["positions"] for point in result["equity"]) <= 2
    with sqlite3.connect(str(fixture["database"])) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT candidate_id, replay_id, experiment_id
            FROM backtest_trades WHERE run_id=?
            """,
            (result["run_id"],),
        ).fetchall()
    assert len(rows) == 2
    assert {
        (row["candidate_id"], row["replay_id"], row["experiment_id"])
        for row in rows
    } == {
        (
            fixture["candidate_id"],
            fixture["replay_id"],
            fixture["experiment_id"],
        )
    }


def test_explicit_experiment_id_prevents_latest_run_fallback(tmp_path):
    fixture = _replay_fixture(tmp_path, name="lineage", codes=("000001",))
    second_experiment_id = record_experiment(
        fixture["database"],
        code_version="trial-backtest-test",
        data_snapshot_hash=fixture["snapshot"]["artifact_sha256"],
        search_space={"name": ["second"]},
        seed=7,
        objective_config={"metric": "final_equity", "direction": "maximize"},
        candidate_id=fixture["candidate_id"],
        replay_id=fixture["replay_id"],
        artifact_paths={
            "snapshot": fixture["snapshot"]["artifact_path"],
            "replay": fixture["replay"]["artifact_path"],
        },
    )
    assert second_experiment_id != fixture["experiment_id"]
    bars = _single_code_bars()

    with pytest.raises(TrialBacktestError, match="multiple experiments"):
        run_trial_backtest(
            fixture["database"],
            fixture["replay_id"],
            fixture["candidate_id"],
            bars,
        )

    first = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        bars,
        experiment_id=fixture["experiment_id"],
    )
    second = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        bars,
        experiment_id=second_experiment_id,
    )

    assert first["run_id"] != second["run_id"]
    with sqlite3.connect(str(fixture["database"])) as connection:
        runs = connection.execute(
            "SELECT experiment_id FROM backtest_runs ORDER BY experiment_id"
        ).fetchall()
    assert [row[0] for row in runs] == sorted(
        [fixture["experiment_id"], second_experiment_id]
    )


@pytest.mark.parametrize(
    ("case", "expected_reason", "expected_last_date", "expected_last_price"),
    (
        ("suspended", "suspended", "2024-01-04", 11.0),
        ("limit_down", "limit_down", "2024-01-04", 11.0),
        ("zero_volume", "zero_volume", "2024-01-04", 11.0),
        ("missing_bar", "missing_bar", "2024-01-04", 11.0),
        ("entry_final", "entry_at_final_session", "2024-01-03", 10.5),
    ),
)
def test_terminal_open_position_semantics(
    case, expected_reason, expected_last_date, expected_last_price
):
    scores = [
        {"decision_date": "2024-01-02", "code": "000001", "score": 1.0}
    ]
    bars = {
        "000001": [
            _bar("2024-01-01", 10.0, 10.2, 9.8, 10.0),
            _bar("2024-01-02", 10.0, 10.2, 9.8, 10.0),
            _bar("2024-01-03", 10.0, 10.6, 9.9, 10.5),
            _bar("2024-01-04", 11.0, 11.2, 10.8, 11.0),
        ]
    }
    if case == "missing_bar":
        scores.append(
            {"decision_date": "2024-01-05", "code": "000001", "score": -1.0}
        )
    elif case == "entry_final":
        bars["000001"] = bars["000001"][:3]
    else:
        bars["000001"][-1].update(
            {
                "is_suspended": case == "suspended",
                "is_limit_down": case == "limit_down",
                "volume": 0.0 if case == "zero_volume" else 1000.0,
            }
        )

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

    final_point = result["mark_to_market_equity"][-1]
    open_position = result["open_positions"][0]
    assert result["mark_to_market_equity"] is result["equity"]
    assert result["trades"] == []
    assert result["closed_trade_stats"]["trade_count"] == 0
    assert result["metrics"]["trade_count"] == 0
    assert final_point["total_assets"] == final_point["cash"] + final_point["market_value"]
    assert final_point["total_assets"] == final_point["mark_to_market_equity"]
    assert final_point["total_assets"] == pytest.approx(
        final_point["cash"] + final_point["open_cost"] + final_point["unrealized_pnl"]
    )
    assert open_position["code"] == "000001"
    assert open_position["unsellable_reason"] == expected_reason
    assert open_position["last_date"] == expected_last_date
    assert open_position["last_price"] == pytest.approx(expected_last_price)
    assert open_position["unrealized_pnl"] == pytest.approx(
        open_position["last_price"] * open_position["shares"] - open_position["cost"]
    )
    assert result["liquidated_equity"] == pytest.approx(final_point["total_assets"])


def test_sellable_terminal_position_uses_liquidated_equity_once():
    result = simulate_trial(
        scores=[{"decision_date": "2024-01-02", "code": "000001", "score": 1.0}],
        bars={
            "000001": [
                _bar("2024-01-01", 10.0, 10.2, 9.8, 10.0),
                _bar("2024-01-02", 10.0, 10.2, 9.8, 10.0),
                _bar("2024-01-03", 10.0, 10.6, 9.9, 10.5),
                _bar("2024-01-04", 11.0, 11.2, 10.8, 11.0),
            ]
        },
        config={
            "initial_cash": 100_000.0,
            "position_size_pct": 0.5,
            "lot_size": 1,
            "max_hold_sessions": 10,
            "commission_rate": 0.0,
            "min_commission": 0.0,
            "stamp_duty_rate": 0.0,
            "transfer_fee_rate": 0.0,
            "slippage_bps": 50.0,
            "max_volume_participation": 1.0,
            "stop_atr_multiple": 100.0,
            "target_atr_multiple": 100.0,
        },
    )

    assert result["open_positions"] == []
    assert result["trades"][-1]["exit_reason"] == "end_of_backtest"
    assert result["closed_trade_stats"]["trade_count"] == 1
    assert result["liquidated_equity"] < result["mark_to_market_equity"][-1]["total_assets"]


def test_open_positions_are_persisted_separately_from_closed_trades(tmp_path):
    fixture = _replay_fixture(tmp_path, name="open-position", codes=("000001",))
    result = run_trial_backtest(
        fixture["database"],
        fixture["replay_id"],
        fixture["candidate_id"],
        {
            "000001": [
                _bar("2024-01-01", 90.0, 91.0, 89.0, 90.0),
                _bar("2024-01-02", 95.0, 96.0, 94.0, 95.0),
                _bar(
                    "2024-01-03",
                    95.0,
                    96.0,
                    94.0,
                    95.0,
                    is_suspended=True,
                ),
            ]
        },
        config={
            "initial_cash": 100_000.0,
            "position_size_pct": 0.5,
            "lot_size": 1,
            "max_hold_sessions": 10,
        },
    )

    with sqlite3.connect(str(fixture["database"])) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT * FROM backtest_runs WHERE run_id=?", (result["run_id"],)
        ).fetchone()
        trade_count = connection.execute(
            "SELECT COUNT(*) FROM backtest_trades WHERE run_id=?", (result["run_id"],)
        ).fetchone()[0]

    assert trade_count == 0
    assert json.loads(run["mark_to_market_equity_json"]) == result["mark_to_market_equity"]
    assert run["liquidated_equity"] == pytest.approx(result["liquidated_equity"])
    assert json.loads(run["closed_trade_stats_json"]) == result["closed_trade_stats"]
    assert json.loads(run["open_positions_json"]) == result["open_positions"]


def test_market_views_use_raw_execution_and_adjusted_benchmark_returns():
    raw_bars = {
        "000001": [
            _bar("2023-12-29", 100.0, 101.0, 99.0, 100.0, volume=10_000.0),
            _bar("2024-01-01", 100.0, 101.0, 99.0, 100.0, volume=10_000.0),
            _bar("2024-01-02", 50.0, 50.5, 49.5, 50.0, volume=20_000.0),
            _bar("2024-01-03", 50.0, 51.0, 49.0, 50.0, volume=20_000.0),
        ]
    }
    adjusted_bars = {
        "000001": [
            _bar("2023-12-29", 100.0, 101.0, 99.0, 100.0, volume=10_000.0),
            _bar("2024-01-01", 100.0, 101.0, 99.0, 100.0, volume=10_000.0),
            _bar("2024-01-02", 100.0, 101.0, 99.0, 100.0, volume=10_000.0),
            _bar("2024-01-03", 100.0, 102.0, 98.0, 100.0, volume=10_000.0),
        ]
    }

    result = simulate_trial(
        scores=[{"decision_date": "2024-01-01", "code": "000001", "score": 1.0}],
        bars={"price_basis": "raw", "bars": raw_bars},
        research_bars={"price_basis": "total_return", "bars": adjusted_bars},
        config={
            "initial_cash": 100_000.0,
            "position_size_pct": 0.5,
            "lot_size": 100,
            "max_hold_sessions": 1,
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

    assert result["execution_price_basis"] == "raw"
    assert result["research_price_basis"] == "total_return"
    assert result["metrics"]["benchmark_price_basis"] == "total_return"
    assert result["metrics"]["benchmark_total_return"] == 0.0
    assert result["trades"][0]["intended_entry"] == 50.0
    assert result["trades"][0]["entry_price"] == 50.0
    assert result["trades"][0]["shares"] == 1000


def test_market_views_fail_closed_on_mixed_or_raw_research_basis():
    raw = {
        "000001": [
            _bar("2024-01-01", 10.0, 10.2, 9.8, 10.0),
            _bar("2024-01-02", 10.0, 10.2, 9.8, 10.0),
        ]
    }
    with pytest.raises(TrialBacktestError, match="execution.*raw|price_basis=raw|expected 'raw'"):
        normalize_market_views({"price_basis": "adjusted", "bars": raw})
    with pytest.raises(TrialBacktestError, match="research.*adjusted|total_return"):
        normalize_market_views(
            {"price_basis": "raw", "bars": raw},
            research_bars={"price_basis": "raw", "bars": raw},
        )
    mixed_rows = deepcopy(raw)
    mixed_rows["000001"][1]["price_basis"] = "adjusted"
    with pytest.raises(TrialBacktestError, match="mix.*price bases"):
        normalize_market_views({"price_basis": "raw", "bars": mixed_rows})


def test_raw_limit_flags_block_fills_despite_adjusted_research_continuity():
    raw_bars = {
        "000001": [
            _bar("2023-12-29", 100.0, 101.0, 99.0, 100.0),
            _bar("2024-01-01", 100.0, 101.0, 99.0, 100.0),
            _bar(
                "2024-01-02",
                110.0,
                110.0,
                109.0,
                110.0,
                is_limit_up=True,
            ),
            _bar("2024-01-03", 110.0, 111.0, 109.0, 110.0),
        ]
    }
    result = simulate_trial(
        scores=[{"decision_date": "2024-01-01", "code": "000001", "score": 1.0}],
        bars={"price_basis": "raw", "bars": raw_bars},
        research_bars={
            "price_basis": "total_return",
            "bars": deepcopy(raw_bars),
        },
        config={"max_hold_sessions": 1, "lot_size": 100},
    )

    assert result["trades"] == []
    assert result["metrics"]["execution_diagnostics"]["limit_up_blocked"] == 1


def test_benchmark_retains_raw_proxy_and_audits_sanitized_exclusions():
    result = simulate_trial(
        scores=[
            {"decision_date": "2024-01-01", "code": "999999", "score": 1.0}
        ],
        bars={
            "000001": [
                _bar("2024-01-01", 1.0, 1.0, 1.0, 1.0),
                _bar("2024-01-02", 2.0, 2.0, 2.0, 2.0),
            ],
            "000002": [
                _bar("2024-01-01", 1.0, 1.0, 1.0, 1.0),
                _bar("2024-01-02", 1.0, 1.0, 1.0, 1.0),
            ],
        },
        config={"initial_cash": 100_000.0, "lot_size": 100},
    )
    metrics = result["metrics"]

    assert metrics["benchmark_raw_total_return"] == 0.5
    assert metrics["benchmark_total_return"] == 0.5
    assert metrics["benchmark_sanitized_total_return"] == 0.0
    assert metrics["benchmark_raw_curve"][-1]["benchmark_return"] == 0.5
    assert metrics["benchmark_sanitized_curve"][-1]["benchmark_return"] == 0.0
    assert metrics["benchmark_exclusions"] == [
        {
            "date": "2024-01-02",
            "code": "000001",
            "raw_return": 1.0,
            "exclusion_reason": "absolute_return_gt_0.50",
        }
    ]
    policy = metrics["benchmark_anomaly_policy"]
    assert policy["strategy"] == policy["benchmark"] == "retain_all_finite_returns"
    assert policy["sanitized_auxiliary"]["role"] == "audit_only"
    assert result["return_anomaly_policy"] == policy


def test_sanitized_benchmark_threshold_excludes_only_returns_above_half():
    result = simulate_trial(
        scores=[
            {"decision_date": "2024-01-01", "code": "999999", "score": 1.0}
        ],
        bars={
            "000001": [
                _bar("2024-01-01", 1.0, 1.0, 1.0, 1.0),
                _bar("2024-01-02", 1.5, 1.5, 1.5, 1.5),
            ],
            "000002": [
                _bar("2024-01-01", 1.0, 1.0, 1.0, 1.0),
                _bar("2024-01-02", 1.0, 1.0, 1.0, 1.0),
            ],
        },
        config={"initial_cash": 100_000.0, "lot_size": 100},
    )

    assert result["metrics"]["benchmark_total_return"] == 0.25
    assert result["metrics"]["benchmark_sanitized_total_return"] == 0.25
    assert result["metrics"]["benchmark_exclusions"] == []


def test_short_sample_marks_statistical_metrics_unavailable_without_hiding_raw_return():
    result = simulate_trial(
        scores=[
            {"decision_date": "2024-01-01", "code": "999999", "score": 1.0}
        ],
        bars={
            "000001": [
                _bar("2024-01-01", 10.0, 10.2, 9.8, 10.0),
                _bar("2024-01-02", 10.1, 10.3, 9.9, 10.2),
            ]
        },
        config={"initial_cash": 100_000.0, "lot_size": 100},
    )
    metrics = result["metrics"]

    assert metrics["trading_days"] == 2
    assert metrics["trade_count"] == 0
    assert metrics["total_return"] == 0.0
    assert metrics["date_range"] == {
        "start": "2024-01-01",
        "end": "2024-01-02",
    }
    assert metrics["annualized_return"] is None
    assert metrics["sharpe"] is None
    assert metrics["sortino"] is None
    assert metrics["unavailable_reason"] == "minimum_observation_sessions_not_met"
