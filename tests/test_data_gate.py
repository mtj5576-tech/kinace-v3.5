import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from kinace_v3.data_gate import evaluate_data_gate, write_data_gate


def _create_v2_fixture(
    path: Path,
    evaluation_rows_path: Path,
    *,
    decision_dates: int,
    evaluation_rows: int,
    nonzero_trades: int,
    raw_replay_inputs: bool,
) -> None:
    raw_marker = {
        "features": {"turnover": 2.1},
        "penalty_inputs": {"rsi": 55.0, "kdj": 62.0, "amplitude": 0.02},
        "universe": {"included": True},
        "tradability": {"is_suspended": False, "is_limit_up": False},
        "labels": {"forward_return": 0.01},
        "model_config": {"version": "synthetic"},
    }

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE recommendation_snapshots (
                snapshot_id INTEGER PRIMARY KEY,
                decision_date TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE recommendation_items (
                item_id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                feature_json TEXT NOT NULL,
                recommendation_json TEXT NOT NULL
            );
            CREATE TABLE model_windows (window_id INTEGER PRIMARY KEY);
            CREATE TABLE model_candidates (candidate_id INTEGER PRIMARY KEY);
            CREATE TABLE model_promotions (promotion_id INTEGER PRIMARY KEY);
            CREATE TABLE backtest_runs (run_id TEXT PRIMARY KEY);
            CREATE TABLE backtest_trades (
                trade_id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                shares INTEGER
            );
            """
        )

        first_date = date(2024, 1, 1)
        for index in range(decision_dates):
            decision_date = (first_date + timedelta(days=index)).isoformat()
            payload = {"date": decision_date, "candidates": [{"code": f"{index:06d}"}]}
            feature = {"close_location_5": 0.8, "gap": 0.01}
            if raw_replay_inputs:
                payload["raw_replay_inputs"] = raw_marker
                feature["raw_replay_inputs"] = raw_marker
            connection.execute(
                "INSERT INTO recommendation_snapshots VALUES (?, ?, ?)",
                (index + 1, decision_date, json.dumps(payload)),
            )
            connection.execute(
                "INSERT INTO recommendation_items VALUES (?, ?, ?, ?, ?)",
                (
                    index + 1,
                    index + 1,
                    f"{index:06d}",
                    json.dumps(feature),
                    "{}",
                ),
            )

        for index in range(3):
            connection.execute("INSERT INTO backtest_runs VALUES (?)", (f"run-{index}",))
        for index in range(nonzero_trades):
            connection.execute(
                "INSERT INTO backtest_trades (run_id, shares) VALUES (?, ?)",
                (f"run-{index % 3}", 100),
            )
        connection.execute(
            "INSERT INTO backtest_trades (run_id, shares) VALUES ('run-0', 0)"
        )

    with evaluation_rows_path.open("w", encoding="utf-8", newline="\n") as handle:
        for index in range(evaluation_rows):
            handle.write(
                json.dumps(
                    {
                        "date": (first_date + timedelta(days=index % max(decision_dates, 1))).isoformat(),
                        "code": f"{index:06d}",
                        "forward_return": 0.01,
                    }
                )
                + "\n"
            )


def _evaluate(tmp_path: Path, **fixture_options):
    database = tmp_path / "v2.sqlite3"
    evaluation_rows = tmp_path / "evaluation_rows.jsonl"
    v3_root = tmp_path / "v3-data"
    _create_v2_fixture(database, evaluation_rows, **fixture_options)
    return evaluate_data_gate(
        v2_database_path=database,
        evaluation_rows_path=evaluation_rows,
        v3_data_root=v3_root,
        v3_database_path=v3_root / "kinace_v3_1.sqlite3",
    )


def test_blocked_synthetic_state_reports_all_failed_point_in_time_evidence(tmp_path):
    result = _evaluate(
        tmp_path,
        decision_dates=1,
        evaluation_rows=0,
        nonzero_trades=0,
        raw_replay_inputs=False,
    )

    assert result["status"] == "blocked_insufficient_point_in_time_data"
    assert result["ready"] is False
    assert result["counts"]["independent_decision_dates"] == 1
    assert result["counts"]["evaluation_rows"] == 0
    assert result["counts"]["nonzero_trades"] == 0
    assert result["raw_replay_inputs_available"] is False
    assert set(result["failed_hard_gates"]) == {
        "independent_decision_dates",
        "labeled_evaluation_rows",
        "raw_replay_inputs",
        "nonzero_backtest_trades",
    }


def test_ready_synthetic_state_passes_only_with_complete_evidence(tmp_path):
    result = _evaluate(
        tmp_path,
        decision_dates=372,
        evaluation_rows=372,
        nonzero_trades=30,
        raw_replay_inputs=True,
    )

    assert result["status"] == "ready"
    assert result["ready"] is True
    assert result["failed_hard_gates"] == []
    assert all(gate["passed"] for gate in result["hard_gates"])
    assert result["counts"] == {
        "independent_decision_dates": 372,
        "snapshots": 372,
        "recommendation_items": 372,
        "distinct_codes": 372,
        "evaluation_rows": 372,
        "model_windows": 0,
        "candidates": 0,
        "promotions": 0,
        "backtest_runs": 3,
        "nonzero_trades": 30,
    }

    output = tmp_path / "data_gate.json"
    write_data_gate(result, output)
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "ready"


@pytest.mark.parametrize(
    ("fixture_options", "failed_gate"),
    [
        (
            {
                "decision_dates": 371,
                "evaluation_rows": 372,
                "nonzero_trades": 30,
                "raw_replay_inputs": True,
            },
            "independent_decision_dates",
        ),
        (
            {
                "decision_dates": 372,
                "evaluation_rows": 0,
                "nonzero_trades": 30,
                "raw_replay_inputs": True,
            },
            "labeled_evaluation_rows",
        ),
        (
            {
                "decision_dates": 372,
                "evaluation_rows": 372,
                "nonzero_trades": 30,
                "raw_replay_inputs": False,
            },
            "raw_replay_inputs",
        ),
        (
            {
                "decision_dates": 372,
                "evaluation_rows": 372,
                "nonzero_trades": 29,
                "raw_replay_inputs": True,
            },
            "nonzero_backtest_trades",
        ),
    ],
)
def test_any_failed_hard_gate_blocks_readiness(tmp_path, fixture_options, failed_gate):
    result = _evaluate(tmp_path, **fixture_options)

    assert result["ready"] is False
    assert result["status"] == "blocked_insufficient_point_in_time_data"
    assert failed_gate in result["failed_hard_gates"]
