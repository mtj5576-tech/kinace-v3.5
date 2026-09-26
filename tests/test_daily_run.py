from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kinace_v3.daily_run import DailyRunError, _run_snapshot_stage, run_daily
from kinace_v3.feature_snapshot import FEATURE_NAMES, create_feature_snapshot, write_feature_snapshot
from kinace_v3.score_replay import replay_candidate


def _model_config(version: str = "daily-run-test") -> dict:
    return {
        "version": version,
        "mode": "daily_test",
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


def _snapshot(tmp_path: Path, decision_date: str) -> dict:
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
                "labels": {"forward_return": 0.01},
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
        tmp_path / f"{decision_date}.json",
    )


def test_daily_run_orchestrates_stages_and_is_idempotent(tmp_path, monkeypatch):
    decision_date = "2024-01-02"
    snapshot_dir = tmp_path / "snapshots"
    snapshot = _snapshot(snapshot_dir, decision_date)
    replay = replay_candidate(
        snapshot,
        _model_config("daily-run-selected"),
        tmp_path / "selected-replay.json",
    )
    database = tmp_path / "v3.sqlite3"
    attempts = {"gate": 0}
    fake_bars = {
        "000001": [
            {"code": "000001", "trade_date": "2024-01-01", "high": 10.0, "low": 9.0, "close": 9.5},
            {"code": "000001", "trade_date": "2024-01-02", "high": 11.0, "low": 10.0, "close": 10.5},
        ],
        "000002": [
            {"code": "000002", "trade_date": "2024-01-01", "high": 10.0, "low": 9.0, "close": 9.5},
            {"code": "000002", "trade_date": "2024-01-02", "high": 11.0, "low": 10.0, "close": 10.5},
        ],
    }
    monkeypatch.setattr("kinace_v3.daily_run.load_market_bars", lambda database: fake_bars)

    def snapshot_runner(**kwargs):
        return {
            "snapshot_count": 1,
            "row_count": snapshot["payload"]["row_count"],
            "snapshot_id": snapshot["snapshot_id"],
        }

    def gate_runner(**kwargs):
        attempts["gate"] += 1
        if attempts["gate"] == 1:
            raise RuntimeError("transient gate failure")
        return {
            "status": "release_ready",
            "ready": True,
            "development_only": False,
            "production_eligible": True,
        }

    def search_runner(**kwargs):
        return {
            "artifact_type": "kinace_v3_search_run",
            "status": "research_only",
            "candidate_count": 1,
            "selected_candidate_id": replay["payload"]["candidate_id"],
            "candidates": [
                {
                    "candidate_id": replay["payload"]["candidate_id"],
                    "replay_id": replay["replay_id"],
                    "experiment_id": "experiment-1",
                    "replay_path": replay["artifact_path"],
                    "metrics": {"mean_rank_ic": 0.1},
                }
            ],
        }

    def portfolio_runner(*args, **kwargs):
        return {
            "run_id": "portfolio-run-1",
            "output_path": str(tmp_path / "portfolio.json"),
            "metrics": {
                "trade_count": 4,
                "final_equity": 101_000.0,
                "total_return": 0.01,
                "max_drawdown": 0.02,
                "win_rate": 0.5,
                "profit_factor": 1.2,
            },
        }

    result = run_daily(
        database=database,
        decision_date=decision_date,
        candidate_config=_model_config(),
        snapshot_dir=snapshot_dir,
        output_dir=tmp_path / "daily",
        report_dir=tmp_path / "reports",
        sync_market=False,
        retries=1,
        retry_delay_seconds=0,
        runner_overrides={
            "snapshots": snapshot_runner,
            "gate": gate_runner,
            "search": search_runner,
            "portfolio": portfolio_runner,
        },
    )

    assert result["status"] == "completed"
    assert result["stages"]["sync"]["status"] == "skipped"
    assert result["stages"]["snapshots"]["status"] == "completed"
    assert result["stages"]["gate"]["status"] == "completed"
    assert result["stages"]["search"]["status"] == "completed"
    assert result["stages"]["strategy"]["status"] == "completed"
    assert result["stages"]["portfolio"]["status"] == "completed"
    assert len(result["strategy"]["picks"]) == 2
    assert result["strategy"]["trade_plan"]["atr_period"] == 14
    assert result["strategy"]["trade_plan"]["price_basis"] == "decision_close"
    assert result["strategy"]["picks"][0]["atr"] == 1.25
    assert result["strategy"]["picks"][0]["stop_loss"] == 8.0
    assert result["strategy"]["picks"][0]["take_profit"] == 14.25
    assert Path(result["manifest_path"]).is_file()
    assert Path(result["report_path"]).is_file()
    assert not (tmp_path / "daily" / ".daily-run.lock").exists()

    second = run_daily(
        database=database,
        decision_date=decision_date,
        candidate_config=_model_config(),
        snapshot_dir=snapshot_dir,
        output_dir=tmp_path / "daily",
        report_dir=tmp_path / "reports",
        sync_market=False,
        retries=1,
        retry_delay_seconds=0,
        runner_overrides={
            "snapshots": snapshot_runner,
            "gate": gate_runner,
            "search": search_runner,
            "portfolio": portfolio_runner,
        },
    )

    assert second["run_id"] == result["run_id"]
    with sqlite3.connect(str(database)) as connection:
        row = connection.execute("SELECT COUNT(*), MIN(status) FROM daily_runs").fetchone()
    assert row == (1, "completed")
    saved = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert saved["run_id"] == result["run_id"]
    assert saved["production_changed"] is False


def test_daily_run_skips_search_and_portfolio_when_gate_is_blocked(tmp_path):
    decision_date = "2024-01-02"
    snapshot_dir = tmp_path / "snapshots"
    snapshot = _snapshot(snapshot_dir, decision_date)
    database = tmp_path / "v3.sqlite3"

    result = run_daily(
        database=database,
        decision_date=decision_date,
        candidate_config=_model_config(),
        snapshot_dir=snapshot_dir,
        output_dir=tmp_path / "daily",
        report_dir=tmp_path / "reports",
        sync_market=False,
        runner_overrides={
            "snapshots": lambda **kwargs: {
                "snapshot_count": 1,
                "row_count": snapshot["payload"]["row_count"],
                "snapshot_id": snapshot["snapshot_id"],
            },
            "gate": lambda **kwargs: {
                "status": "blocked_insufficient_point_in_time_data",
                "ready": False,
                "failed_hard_gates": ["nonzero_candidate_specific_trades"],
            },
        },
    )

    assert result["status"] == "blocked_data_gate"
    assert result["stages"]["search"]["status"] == "skipped"
    assert result["stages"]["portfolio"]["status"] == "skipped"
    assert Path(result["manifest_path"]).is_file()
    assert Path(result["report_path"]).is_file()


def test_postclose_uses_development_gate_for_operational_strategy(tmp_path):
    decision_date = "2024-01-02"
    snapshot_dir = tmp_path / "snapshots"
    snapshot = _snapshot(snapshot_dir, decision_date)
    replay = replay_candidate(
        snapshot,
        _model_config("postclose-selected"),
        tmp_path / "postclose-replay.json",
    )
    captured = {}

    def gate_runner(**kwargs):
        captured["gate_mode"] = kwargs["mode"]
        return {
            "status": "development_ready",
            "ready": True,
            "development_only": True,
            "development_eligible": True,
        }

    def search_runner(**kwargs):
        captured["search_development"] = kwargs["development"]
        return {
            "artifact_type": "kinace_v3_search_run",
            "status": "research_only",
            "candidate_count": 1,
            "selected_candidate_id": replay["payload"]["candidate_id"],
            "candidates": [
                {
                    "candidate_id": replay["payload"]["candidate_id"],
                    "replay_id": replay["replay_id"],
                    "experiment_id": "experiment-1",
                    "replay_path": replay["artifact_path"],
                    "metrics": {"mean_rank_ic": 0.1},
                }
            ],
        }

    result = run_daily(
        database=tmp_path / "v3.sqlite3",
        decision_date=decision_date,
        candidate_config=_model_config(),
        snapshot_dir=snapshot_dir,
        output_dir=tmp_path / "daily",
        report_dir=tmp_path / "reports",
        gate_output=tmp_path / "gate.json",
        run_purpose="postclose",
        sync_market=False,
        runner_overrides={
            "snapshots": lambda **kwargs: {
                "snapshot_count": 1,
                "row_count": snapshot["payload"]["row_count"],
                "snapshot_id": snapshot["snapshot_id"],
            },
            "gate": gate_runner,
            "search": search_runner,
            "portfolio": lambda *args, **kwargs: {
                "run_id": "portfolio-run-1",
                "output_path": str(tmp_path / "portfolio.json"),
                "metrics": {"trade_count": 4},
            },
        },
    )

    assert result["status"] == "completed"
    assert result["stages"]["gate"]["status"] == "completed"
    assert captured == {"gate_mode": "development", "search_development": True}


def test_daily_run_requires_explicit_date_when_sync_is_enabled(tmp_path):
    with pytest.raises(DailyRunError, match="decision-date"):
        run_daily(
            database=tmp_path / "v3.sqlite3",
            candidate_config=_model_config(),
            snapshot_dir=tmp_path / "snapshots",
            output_dir=tmp_path / "daily",
            report_dir=tmp_path / "reports",
            sync_market=True,
        )


def test_daily_run_snapshot_stage_passes_explicit_pit_policy(tmp_path):
    captured = {}

    def snapshot_runner(**kwargs):
        captured.update(kwargs)
        return {
            "snapshot_count": 1,
            "row_count": 1,
            "pit_ready": True,
            "industry_lookahead_safe": True,
            "industry_source": "pit_membership",
            "pit_policy_hash": "a" * 64,
        }

    result = run_daily(
        database=tmp_path / "v3.sqlite3",
        decision_date="2024-01-02",
        candidate_config=_model_config(),
        snapshot_dir=tmp_path / "snapshots",
        output_dir=tmp_path / "daily",
        report_dir=tmp_path / "reports",
        sync_market=False,
        pit_policy_path=tmp_path / "policy.json",
        snapshot_mode="historical",
        runner_overrides={
            "snapshots": snapshot_runner,
            "gate": lambda **kwargs: {
                "status": "blocked",
                "ready": False,
                "failed_hard_gates": ["fixture"],
            },
        },
    )

    assert result["status"] == "blocked_data_gate"
    assert captured["pit_policy_path"] == tmp_path / "policy.json"
    assert captured["snapshot_mode"] == "historical"


def test_daily_snapshot_stage_resolves_the_default_historical_policy(tmp_path, monkeypatch):
    captured = {}

    def fake_build_history_snapshots(*args, **kwargs):
        captured.update(kwargs)
        return {"snapshot_count": 1}

    monkeypatch.setattr(
        "kinace_v3.daily_run.build_history_snapshots",
        fake_build_history_snapshots,
    )

    result = _run_snapshot_stage(
        database=tmp_path / "v3.sqlite3",
        snapshot_dir=tmp_path / "snapshots",
        start_date="2024-01-02",
        end_date="2024-01-02",
        model_config=_model_config(),
        min_history=20,
        pit_policy=None,
        pit_policy_path=None,
        snapshot_mode="historical",
    )

    assert result == {"snapshot_count": 1}
    assert captured["pit_policy"]["industry_mode"] == "pit_membership"
    assert captured["pit_policy_path"].endswith("pit-universe.v32.json")
    assert captured["snapshot_mode"] == "historical"
