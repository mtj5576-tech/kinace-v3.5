from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import kinace_v3.research_loop as research_loop
from kinace_v3.feature_snapshot import FEATURE_NAMES, create_feature_snapshot, write_feature_snapshot
from kinace_v3.holdout_access import holdout_access_count, load_holdout_access_registry
from kinace_v3.research_loop import (
    ResearchLoopError,
    build_walk_forward_windows,
    run_research_loop,
)
from kinace_v3.research_protocol import (
    protocol_candidate_id,
    recompute_evaluation_protocol_hash,
)


def _model_config(version: str) -> dict:
    return {
        "version": version,
        "mode": "research_loop_test",
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


def _dates(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    return [(current + timedelta(days=index)).isoformat() for index in range(count)]


def _write_snapshots(snapshot_dir: Path, decision_dates: list[str]) -> None:
    for decision_date in decision_dates:
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
        write_feature_snapshot(
            create_feature_snapshot(
                decision_date=decision_date,
                candidates=candidates,
                model_config=_model_config("snapshot-model"),
                data_gate={"status": "synthetic", "ready": True},
            ),
            snapshot_dir / f"{decision_date}.json",
        )


def _bars(start: str, count: int) -> dict[str, list[dict]]:
    dates = _dates(start, count)
    result = {}
    for code_offset, code in enumerate(("000001", "000002")):
        result[code] = []
        for index, trade_date in enumerate(dates):
            price = 10.0 + code_offset + index * 0.05
            result[code].append(
                {
                    "trade_date": trade_date,
                    "open": price,
                    "high": price + 0.20,
                    "low": price - 0.10,
                    "close": price + 0.08,
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


def test_walk_forward_windows_reserve_fixed_holdout():
    dates = _dates("2024-01-01", 12)
    schedule = build_walk_forward_windows(
        dates,
        {
            "folds": 2,
            "validation_sessions": 2,
                "test_sessions": 2,
                "min_train_sessions": 3,
                "purge_sessions": 0,
                "embargo_sessions": 0,
                "max_rounds": 1,
            "candidates_per_round": 2,
        },
    )

    assert [item["fold"] for item in schedule["folds"]] == [0, 1]
    assert schedule["holdout"]["start"] == dates[-2]
    assert schedule["holdout"]["end"] == dates[-1]
    with pytest.raises(ResearchLoopError):
        build_walk_forward_windows(dates[:5], {"min_train_sessions": 4})


def test_walk_forward_windows_apply_purge_and_embargo_gaps():
    dates = _dates("2024-01-01", 16)
    schedule = build_walk_forward_windows(
        dates,
        {
            "folds": 1,
            "validation_sessions": 2,
            "test_sessions": 2,
            "min_train_sessions": 4,
            "purge_sessions": 1,
            "embargo_sessions": 1,
            "max_rounds": 1,
            "candidates_per_round": 1,
        },
    )

    fold = schedule["folds"][0]
    assert fold["train_end"] == dates[7]
    assert fold["validation_start"] == dates[10]
    assert fold["validation_end"] == dates[11]
    assert schedule["holdout"]["train_end"] == dates[11]
    assert schedule["holdout"]["start"] == dates[14]


def test_candidate_aggregate_trade_count_controls_validation_status():
    fold_results = [
        {
            "status": "passed",
            "objective_score": 1.0,
            "metrics": {
                "total_return": 0.01,
                "max_drawdown": 0.02,
                "trade_count": count,
            },
        }
        for count in (5, 5, 20)
    ]

    result = research_loop._aggregate_fold_results(
        fold_results,
        trade_count_policy={},
    )
    inactive = research_loop._aggregate_fold_results(
        [
            {
                **fold_results[0],
                "metrics": {
                    **fold_results[0]["metrics"],
                    "trade_count": 0,
                },
            },
            fold_results[1],
            fold_results[2],
        ],
        trade_count_policy={},
    )

    assert result["status"] == "validated"
    assert result["trade_count_adjudication"]["candidate_trade_count"] == 30
    assert result["trade_count_adjudication"]["active_fold_rate"] == 1.0
    assert inactive["status"] == "rejected"
    assert inactive["trade_count_adjudication"]["active_fold_rate"] < 1.0


def test_candidate_weight_fit_reuses_window_ic_cache(tmp_path, monkeypatch):
    calls = 0

    def _fake_ic_summary(
        snapshot_dir,
        *,
        start_date,
        end_date,
        label_horizon,
        standardization=None,
        recency_halflife=None,
        recency_mode="direction_and_strength",
        conflict_scale=0.25,
        feature_transform=None,
        snapshots=None,
    ):
        nonlocal calls
        calls += 1
        _fake_ic_summary.calls.append(
            {
                "recency_halflife": recency_halflife,
                "recency_mode": recency_mode,
                "conflict_scale": conflict_scale,
                "feature_transform": feature_transform,
            }
        )
        return (
            {
                feature: research_loop.summarize_ic_series([0.01, 0.01, 0.01])
                for feature in FEATURE_NAMES
            },
            ["2024-01-01"],
        )

    research_loop._RANK_IC_CACHE.clear()
    monkeypatch.setattr(research_loop, "_compute_rank_ic_summary", _fake_ic_summary)
    _fake_ic_summary.calls = []
    base_model = _model_config("cache-test")

    first_model, first_training = research_loop._fit_candidate_weights_cached(
        base_model_config=base_model,
        snapshot_dir=tmp_path / "snapshots",
        start_date="2024-01-01",
        end_date="2024-01-31",
    )
    second_model, second_training = research_loop._fit_candidate_weights_cached(
        base_model_config=base_model,
        snapshot_dir=tmp_path / "snapshots",
        start_date="2024-01-01",
        end_date="2024-01-31",
    )
    neutralized_model = _model_config("cache-test-neutralized")
    neutralized_model["standardization"] = {
        "version": 1,
        "method": "mad_winsorized",
        "mad_multiplier": 3.0,
        "neutralize_industry": True,
        "neutralize_market_cap": False,
    }
    third_model, third_training = research_loop._fit_candidate_weights_cached(
        base_model_config=neutralized_model,
        snapshot_dir=tmp_path / "snapshots",
        start_date="2024-01-01",
        end_date="2024-01-31",
    )
    recency_model = _model_config("cache-test-recency")
    recency_model["training_policy"] = {"recency_halflife_sessions": 60.0}
    fourth_model, fourth_training = research_loop._fit_candidate_weights_cached(
        base_model_config=recency_model,
        snapshot_dir=tmp_path / "snapshots",
        start_date="2024-01-01",
        end_date="2024-01-31",
    )
    transformed_model = _model_config("cache-test-transform")
    transformed_model["feature_transform"] = {
        "log_amount": {
            "mode": "change",
            "lookback_sessions": 5,
        }
    }
    fifth_model, fifth_training = research_loop._fit_candidate_weights_cached(
        base_model_config=transformed_model,
        snapshot_dir=tmp_path / "snapshots",
        start_date="2024-01-01",
        end_date="2024-01-31",
    )

    assert calls == 4
    assert any(
        call["recency_halflife"] == 60.0
        and call["recency_mode"] == "direction_and_strength"
        for call in _fake_ic_summary.calls
    )
    assert _fake_ic_summary.calls[-1]["feature_transform"] == {
        "log_amount": {
            "version": 1,
            "mode": "change",
            "lookback_sessions": 5,
        }
    }
    assert first_model == second_model
    assert first_training == second_training
    assert third_model["standardization"] != first_model["standardization"]
    assert third_training["used_dates"] == first_training["used_dates"]
    assert fourth_model["training_policy"] == {"recency_halflife_sessions": 60.0}
    assert fourth_training["used_dates"] == first_training["used_dates"]
    assert fifth_model["feature_transform"] == transformed_model["feature_transform"]
    assert fifth_training["used_dates"] == first_training["used_dates"]


def test_rank_ic_summary_uses_requested_standardization(tmp_path):
    snapshot_dir = tmp_path / "standardization-snapshots"
    snapshot_dir.mkdir()
    candidates = []
    for index in range(4):
        candidates.append(
            {
                "code": f"00000{index + 1}",
                "raw_inputs": {
                    feature: float(index + 1) + feature_index / 10.0
                    for feature_index, feature in enumerate(FEATURE_NAMES)
                },
                "standardization_inputs": {"industry": "a" if index < 2 else "b"},
                "penalty_inputs": {
                    "rsi": 55.0,
                    "kdj_j": 70.0,
                    "amplitude_5d": 0.01,
                },
                "labels": {"forward_return_5d": float(index + 1)},
                "point_in_time": {"as_of_utc": "2024-01-02T07:00:00Z"},
            }
        )
    write_feature_snapshot(
        create_feature_snapshot(
            decision_date="2024-01-02",
            candidates=candidates,
            model_config=_model_config("standardization-test"),
            data_gate={"status": "synthetic", "ready": True},
        ),
        snapshot_dir / "2024-01-02.json",
    )

    raw_ic, _ = research_loop._compute_rank_ic_summary(
        snapshot_dir,
        start_date="2024-01-02",
        end_date="2024-01-02",
        label_horizon=5,
        standardization={
            "version": 1,
            "method": "raw",
            "mad_multiplier": 3.0,
            "neutralize_industry": False,
            "neutralize_market_cap": False,
        },
    )
    neutralized_ic, _ = research_loop._compute_rank_ic_summary(
        snapshot_dir,
        start_date="2024-01-02",
        end_date="2024-01-02",
        label_horizon=5,
        standardization={
            "version": 1,
            "method": "raw",
            "mad_multiplier": 3.0,
            "neutralize_industry": True,
            "neutralize_market_cap": False,
        },
    )

    assert raw_ic["gap"]["mean_ic"] == pytest.approx(1.0)
    assert neutralized_ic["gap"]["mean_ic"] < raw_ic["gap"]["mean_ic"]


def test_research_loop_generates_rounds_and_registers_challenger(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    decision_dates = _dates("2024-01-01", 14)
    _write_snapshots(snapshot_dir, decision_dates)
    database = tmp_path / "v3.sqlite3"
    payload = {
        "candidates": [
            {
                "model_config": _model_config("loop-low-risk"),
                "backtest_config": _backtest_config(0.20),
            },
            {
                "model_config": _model_config("loop-high-risk"),
                "backtest_config": _backtest_config(0.50),
            },
        ],
        "backtest_search_space": {
            "position_size_pct": [0.15, 0.20, 0.50, 0.80]
        },
        "objective": {
            "hard_gates": {
                "min_trade_count": 1,
                "max_drawdown": 1.0,
                "min_profit_factor": 0.0,
                "min_total_return": -1.0,
                "min_win_rate": 0.0,
            }
        },
        "walk_forward": {
            "folds": 2,
            "validation_sessions": 3,
            "test_sessions": 4,
            "min_train_sessions": 3,
            "purge_sessions": 0,
            "embargo_sessions": 0,
            "max_rounds": 2,
            "candidates_per_round": 2,
        },
    }

    result = run_research_loop(
        database,
        payload,
        snapshot_dir=snapshot_dir,
        bars=_bars("2023-12-01", 62),
        output_dir=tmp_path / "research",
        report_dir=tmp_path / "reports",
        holdout_access_registry_path=tmp_path / "holdout-access-registry.json",
        stability_config={
            "candidate_min_trade_count": 1,
            "fold_min_trade_count": 1,
            "min_active_fold_rate": 1.0,
        },
    )

    assert result["status"] == "completed"
    assert result["round_count"] == 2
    assert result["candidate_count"] >= 2
    assert result["holdout_status"] == "challenger_ready"
    assert result["holdout_access_read_count"] == 1
    assert result["holdout_access_id"]
    assert holdout_access_count(tmp_path / "holdout-access-registry.json") == 1
    assert result["candidate_audit_required"] is True
    assert result["candidate_registration_status"] == "awaiting_candidate_audit"
    assert result["production_changed"] is False
    assert result["evaluation_protocol_hash"] == (
        recompute_evaluation_protocol_hash(result["evaluation_protocol"])
    )
    stability_settings = result["evaluation_protocol"]["protocol_identity"][
        "evaluation_config"
    ]["stability"]
    assert (
        stability_settings["trade_count_policy_version"]
        == "kinace-v3.3-d2-candidate-level-trade-count-v1"
    )
    assert stability_settings["candidate_min_trade_count"] == 1
    assert stability_settings["fold_min_trade_count"] == 1
    assert stability_settings["min_active_fold_rate"] == 1.0
    holdout_policy = result["evaluation_protocol"]["protocol_identity"][
        "evaluation_config"
    ]["holdout_policy"]
    assert (
        holdout_policy["version"]
        == "kinace-v3.3-holdout-min-trade-extension-v1"
    )
    assert holdout_policy["initial_holdout_sessions"] == 60
    assert holdout_policy["max_total_sessions"] == 120
    assert result["best_candidate"]["protocol_candidate_id"] == protocol_candidate_id(
        result["evaluation_protocol_hash"],
        result["best_candidate"]["signature"],
    )
    assert Path(result["artifact_path"]).is_file()
    assert Path(result["best_challenger_path"]).is_file()
    protocol_binding = result["protocol_binding"]
    assert Path(protocol_binding["effective_protocol_path"]).is_file()
    assert Path(protocol_binding["candidate_manifest_path"]).is_file()
    manifest = json.loads(
        Path(protocol_binding["candidate_manifest_path"]).read_text(encoding="utf-8")
    )
    assert manifest["evaluation_protocol_hash"] == result["evaluation_protocol_hash"]
    assert manifest["observed_execution"]["candidate_count"] >= 2

    with sqlite3.connect(str(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_loop_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_candidate_rounds").fetchone()[0] >= 2
        assert connection.execute("SELECT COUNT(*) FROM research_candidates").fetchone()[0] >= 2
        stage = connection.execute(
            "SELECT stage FROM research_candidates WHERE candidate_signature=?",
            (result["best_candidate"]["signature"],),
        ).fetchone()[0]
    assert stage == "awaiting_candidate_audit"


def test_research_loop_freezes_holdout_when_only_trade_count_fails(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    decision_dates = _dates("2024-01-01", 66)
    _write_snapshots(snapshot_dir, decision_dates)
    database = tmp_path / "v3.sqlite3"
    payload = {
        "candidates": [
            {
                "model_config": _model_config("holdout-extension-low-frequency"),
                "backtest_config": _backtest_config(0.20),
            }
        ],
        "objective": {
            "hard_gates": {
                "min_trade_count": 3,
                "max_drawdown": 1.0,
                "min_profit_factor": 0.0,
                "min_total_return": -1.0,
                "min_win_rate": 0.0,
            }
        },
        "walk_forward": {
            "folds": 1,
            "validation_sessions": 3,
            "test_sessions": 60,
            "min_train_sessions": 3,
            "purge_sessions": 0,
            "embargo_sessions": 0,
            "max_rounds": 1,
            "candidates_per_round": 1,
        },
        "holdout_policy": {
            "initial_holdout_sessions": 60,
            "max_total_sessions": 120,
        },
    }

    def _runner(
        database,
        model_config,
        snapshot_dir,
        *,
        start_date,
        end_date,
        bars,
        config,
        output_path,
        code_version,
    ):
        current = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        sessions = (end - current).days + 1
        trade_count = 2 if sessions == 60 else 5
        trades = [
            {
                "code": f"000{index:03d}",
                "decision_date": start_date,
                "entry_date": start_date,
                "exit_date": start_date,
            }
            for index in range(trade_count)
        ]
        return {
            "run_id": f"run-{sessions}",
            "output_path": str(output_path),
            "date_range": {"start": start_date, "end": end_date},
            "metrics": {
                "equity_basis": "mark_to_market",
                "total_return": 0.05,
                "max_drawdown": 0.05,
                "win_rate": 0.55,
                "turnover": 1.0,
                "sharpe": 1.5,
                "profit_factor": 1.4,
                "trade_count": trade_count,
                "trading_days": sessions,
            },
            "trades": trades,
        }

    result = run_research_loop(
        database,
        payload,
        snapshot_dir=snapshot_dir,
        bars=_bars("2023-12-01", 100),
        output_dir=tmp_path / "research",
        report_dir=tmp_path / "reports",
        holdout_registry_path=tmp_path / "holdout-registry.json",
        holdout_access_registry_path=tmp_path / "holdout-access-registry.json",
        stability_config={
            "candidate_min_trade_count": 1,
            "fold_min_trade_count": 1,
            "min_active_fold_rate": 1.0,
        },
        portfolio_runner=_runner,
    )

    assert result["holdout_status"] == "holdout_extension_pending"
    assert result["candidate_audit_required"] is False
    assert result["candidate_registration_status"] == "awaiting_holdout_extension"
    assert result["holdout"]["status"] == "extension_pending"
    assert result["holdout_access_read_count"] == 1
    assert (
        load_holdout_access_registry(
            tmp_path / "holdout-access-registry.json"
        )["access_count"]
        == 1
    )
    assert Path(result["holdout"]["freeze_path"]).is_file()
    registry = json.loads(
        (tmp_path / "holdout-registry.json").read_text(encoding="utf-8")
    )
    assert registry["records"][0]["status"] == "pending"
    with sqlite3.connect(str(database)) as connection:
        row = connection.execute(
            "SELECT stage, holdout_status FROM research_candidates WHERE candidate_signature=?",
            (result["best_candidate"]["signature"],),
        ).fetchone()
    assert row == ("holdout_extension_pending", "holdout_extension_pending")


def test_research_loop_does_not_read_holdout_before_candidate_is_eligible(
    tmp_path, monkeypatch
):
    snapshot_dir = tmp_path / "snapshots"
    decision_dates = _dates("2024-01-01", 12)
    _write_snapshots(snapshot_dir, decision_dates)
    database = tmp_path / "v3.sqlite3"
    payload = {
        "candidates": [
            {
                "model_config": _model_config("not-eligible"),
                "backtest_config": _backtest_config(0.20),
            }
        ],
        "objective": {
            "hard_gates": {
                "min_trade_count": 1,
                "max_drawdown": 1.0,
                "min_profit_factor": 0.0,
                "min_total_return": 10.0,
                "min_win_rate": 0.0,
            }
        },
        "walk_forward": {
            "folds": 1,
            "validation_sessions": 3,
            "test_sessions": 2,
            "min_train_sessions": 3,
            "purge_sessions": 0,
            "embargo_sessions": 0,
            "max_rounds": 1,
            "candidates_per_round": 1,
        },
    }
    calls: list[tuple[str, str]] = []
    snapshot_load_ranges: list[tuple[str | None, str | None]] = []
    original_discover = research_loop.discover_snapshots

    def _discover(
        snapshot_dir,
        *,
        start_date=None,
        end_date=None,
    ):
        snapshot_load_ranges.append((start_date, end_date))
        return original_discover(
            snapshot_dir,
            start_date=start_date,
            end_date=end_date,
        )

    monkeypatch.setattr(research_loop, "discover_snapshots", _discover)

    def _runner(
        database,
        model_config,
        snapshot_dir,
        *,
        start_date,
        end_date,
        bars,
        config,
        output_path,
        code_version,
    ):
        calls.append((start_date, end_date))
        return {
            "run_id": f"run-{start_date}-{end_date}",
            "output_path": str(output_path),
            "date_range": {"start": start_date, "end": end_date},
            "metrics": {
                "equity_basis": "mark_to_market",
                "total_return": 0.01,
                "max_drawdown": 0.05,
                "win_rate": 0.50,
                "turnover": 1.0,
                "sharpe": 1.0,
                "profit_factor": 1.1,
                "trade_count": 2,
                "trading_days": 3,
            },
            "trades": [],
        }

    access_path = tmp_path / "holdout-access-registry.json"
    result = run_research_loop(
        database,
        payload,
        snapshot_dir=snapshot_dir,
        bars=_bars("2023-12-01", 40),
        output_dir=tmp_path / "research",
        report_dir=tmp_path / "reports",
        holdout_access_registry_path=access_path,
        portfolio_runner=_runner,
    )

    assert result["status"] == "no_eligible_challenger"
    assert result["holdout_eligibility"]["eligible"] is False
    assert result["best_candidate"] is None
    assert result["holdout"] is None
    assert result["holdout_status"] == "not_run"
    assert result["holdout_access_read_count"] == 0
    assert not access_path.exists()
    holdout = result["walk_forward"]["holdout"]
    assert snapshot_load_ranges
    assert all(
        end_date is None or end_date <= holdout["train_end"]
        for _, end_date in snapshot_load_ranges
    )
    assert all(
        (start_date, end_date) != (holdout["start"], holdout["end"])
        for start_date, end_date in calls
    )
