from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from kinace_v3.feature_snapshot import canonical_json_bytes
from kinace_v3.research_decision import (
    ResearchDecisionError,
    load_candidate_oos_returns,
    run_provisional_decision,
)
from kinace_v3.trial_backtest import (
    initialize_trial_backtest_schema,
    trial_config_hash,
)


def _equity(initial: float, returns: list[float]) -> list[dict]:
    rows = []
    value = initial
    current = date(2024, 1, 1)
    for daily_return in returns:
        value *= 1.0 + daily_return
        rows.append({"trade_date": current.isoformat(), "total_assets": value})
        current += timedelta(days=1)
    return rows


def _portfolio_report(
    *,
    run_id: str,
    start: str,
    end: str,
    returns: list[float],
) -> dict:
    return {
        "run_id": run_id,
        "date_range": {"start": start, "end": end},
        "equity": _equity(100_000.0, returns),
        "metrics": {"trading_days": len(returns), "trade_count": len(returns)},
    }


def test_load_candidate_oos_returns_concatenates_folds_and_holdout(tmp_path):
    fold_path = tmp_path / "fold.json"
    holdout_path = tmp_path / "holdout.json"
    fold_path.write_text(
        json.dumps(
            _portfolio_report(
                run_id="fold-run",
                start="2026-01-01",
                end="2026-01-05",
                returns=[0.01, 0.02, 0.01],
            )
        ),
        encoding="utf-8",
    )
    holdout_path.write_text(
        json.dumps(
            _portfolio_report(
                run_id="holdout-run",
                start="2026-01-06",
                end="2026-01-09",
                returns=[0.01, -0.01, 0.02],
            )
        ),
        encoding="utf-8",
    )
    loop_report = tmp_path / "loop.json"
    loop_report.write_text(
        json.dumps(
            {
                "loop_id": "loop-1",
                "best_candidate": {
                    "signature": "candidate-1",
                    "model_config": {"mode": "test"},
                    "backtest_config": {"initial_cash": 100_000.0},
                    "folds": [
                        {
                            "fold": 0,
                            "portfolio_run_id": "fold-run",
                            "portfolio_output_path": str(fold_path),
                            "validation_start": "2026-01-01",
                            "validation_end": "2026-01-05",
                        }
                    ],
                },
                "holdout": {
                    "portfolio_run_id": "holdout-run",
                    "portfolio_output_path": str(holdout_path),
                    "date_range": {"start": "2026-01-06", "end": "2026-01-09"},
                },
            }
        ),
        encoding="utf-8",
    )

    result = load_candidate_oos_returns(loop_report)

    assert result["loop_id"] == "loop-1"
    assert result["candidate_signature"] == "candidate-1"
    assert [segment["kind"] for segment in result["segments"]] == [
        "walk_forward_fold",
        "final_holdout",
    ]
    assert len(result["returns"]) == 4


def test_provisional_decision_is_passed_and_marked_for_rerun(tmp_path):
    returns = [0.011 if index % 2 == 0 else 0.009 for index in range(1_200)]
    current = date(2024, 1, 1)
    end = current + timedelta(days=len(returns) - 1)
    portfolio_path = tmp_path / "holdout.json"
    portfolio_path.write_text(
        json.dumps(
            _portfolio_report(
                run_id="holdout-run",
                start=current.isoformat(),
                end=end.isoformat(),
                returns=returns,
            )
        ),
        encoding="utf-8",
    )
    loop_path = tmp_path / "loop.json"
    loop_path.write_text(
        json.dumps(
            {
                "loop_id": "loop-1",
                "objective": {
                    "constraints": {
                        "min_observation_sessions": {"threshold": 3},
                        "min_trade_count": {"threshold": 1},
                    }
                },
                "best_candidate": {
                    "signature": "candidate-1",
                    "model_config": {"mode": "test"},
                    "backtest_config": {"initial_cash": 100_000.0},
                    "folds": [],
                },
                "holdout": {
                    "portfolio_run_id": "holdout-run",
                    "portfolio_output_path": str(portfolio_path),
                    "date_range": {
                        "start": current.isoformat(),
                        "end": end.isoformat(),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    database = tmp_path / "decision.sqlite3"
    config = {"initial_cash": 100_000.0}
    config_hash = trial_config_hash("model-candidate", config)
    initialize_trial_backtest_schema(database)
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            """
            INSERT INTO model_candidates (
                candidate_id, code_version, candidate_config_json,
                candidate_config_sha256, created_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "model-candidate",
                "test",
                canonical_json_bytes(config, trailing_newline=False).decode(),
                "candidate-sha",
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO score_replays (
                replay_id, candidate_id, snapshot_id, data_snapshot_hash,
                artifact_path, artifact_sha256, candidate_config_json,
                source_snapshot_json, schema_version, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "replay-1",
                "model-candidate",
                "snapshot-1",
                "snapshot-hash",
                "replay.json",
                "replay-sha",
                canonical_json_bytes(config, trailing_newline=False).decode(),
                "{}",
                1,
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO experiments (
                experiment_id, code_version, data_snapshot_hash,
                search_space_json, seed, objective_config_json, candidate_id,
                replay_id, artifact_paths_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "experiment-1",
                "test",
                "snapshot-hash",
                "{}",
                0,
                "{}",
                "model-candidate",
                "replay-1",
                "{}",
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO backtest_runs (
                run_id, experiment_id, candidate_id, replay_id, config_hash,
                code_version, status, start_date, end_date, initial_cash,
                config_json, metrics_json, equity_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-1",
                "experiment-1",
                "model-candidate",
                "replay-1",
                config_hash,
                "test",
                "completed",
                current.isoformat(),
                end.isoformat(),
                100_000.0,
                canonical_json_bytes(config, trailing_newline=False).decode(),
                "{}",
                "[]",
                "2026-01-01T00:00:00Z",
            ),
        )

    result = run_provisional_decision(
        database,
        loop_path,
        target_sharpe=1.0,
        assume_recorded_trials_complete=True,
        bootstrap_iterations=100,
        output_path=tmp_path / "decision.json",
        markdown_path=tmp_path / "decision.md",
    )

    assert result["status"] == "provisional_passed", result["statistics"]["deflated_sharpe_ratio"]
    assert result["provisional"] is True
    assert result["invalidated_by_phase"] == 2
    assert all(result["checks"].values())
    assert Path(result["artifact_path"]).is_file()

    with pytest.raises(ResearchDecisionError, match="frozen finite"):
        run_provisional_decision(
            database,
            loop_path,
            target_sharpe=1.0,
            assume_recorded_trials_complete=False,
            bootstrap_iterations=100,
            output_path=tmp_path / "missing-ledger-decision.json",
        )

    bounded = run_provisional_decision(
        database,
        loop_path,
        target_sharpe=1.0,
        assume_recorded_trials_complete=False,
        unrecorded_trials_upper_bound=1_000,
        bootstrap_iterations=100,
        output_path=tmp_path / "bounded-decision.json",
        markdown_path=tmp_path / "bounded-decision.md",
    )

    assert bounded["status"] == "provisional_passed"
    assert bounded["checks"]["trial_ledger_accounting_complete"] is True
    assert bounded["trial_ledger"]["ledger_complete"] is False
    assert bounded["trial_ledger"]["N_trials_total"] == 1_001
    assert bounded["statistics"]["deflated_sharpe_ratio"]["N_trials"] == 1_001
