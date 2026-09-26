from __future__ import annotations

import math
import sqlite3

from kinace_v3.feature_snapshot import canonical_json_bytes
from kinace_v3.research_statistics import (
    analyze_return_series,
    build_trial_ledger,
    run_research_statistics,
)
from kinace_v3.trial_backtest import (
    initialize_trial_backtest_schema,
    trial_config_hash,
)


def _returns() -> list[float]:
    return [
        0.010,
        -0.004,
        0.006,
        -0.003,
        0.008,
        -0.002,
        0.004,
        0.001,
        -0.005,
        0.007,
        0.003,
        -0.001,
        0.005,
        -0.004,
        0.006,
        0.002,
        -0.003,
        0.004,
        0.001,
        -0.002,
    ]


def test_statistical_machine_is_deterministic_and_derives_required_neff():
    first = analyze_return_series(
        _returns(),
        target_sharpe=1.0,
        N_trials=5,
        bootstrap_iterations=200,
        seed=7,
    )
    second = analyze_return_series(
        _returns(),
        target_sharpe=1.0,
        N_trials=5,
        bootstrap_iterations=200,
        seed=7,
    )

    assert first == second
    inflation = first["autocorrelation"]["variance_inflation"]
    expected_neff = (1.96 / 1.0) ** 2 * 252
    assert first["target_power"]["N_eff_required"] == expected_neff
    assert first["target_power"]["required_sessions"] == math.ceil(
        expected_neff * inflation
    )
    assert first["effective_sample_size"]["N_eff_measured"] > 0
    assert first["deflated_sharpe_ratio"]["computable"] is True
    assert first["binding_gates"]["checks"][
        "bootstrap_confidence_interval_computable"
    ] is True
    interval = first["bootstrap_confidence_interval"]["annualized_sharpe"]
    assert interval["lower"] <= interval["median"] <= interval["upper"]


def test_repeated_trials_increase_dsr_penalty_and_cannot_be_ignored():
    one_trial = analyze_return_series(
        _returns(),
        target_sharpe=1.0,
        N_trials=1,
        bootstrap_iterations=100,
        seed=7,
    )["deflated_sharpe_ratio"]
    many_trials = analyze_return_series(
        _returns(),
        target_sharpe=1.0,
        N_trials=1_000_000,
        bootstrap_iterations=100,
        seed=7,
    )["deflated_sharpe_ratio"]

    assert many_trials["expected_max_sharpe_daily"] > one_trial[
        "expected_max_sharpe_daily"
    ]
    assert many_trials["statistic"] < one_trial["statistic"]
    assert many_trials["p_value"] > one_trial["p_value"]


def test_short_samples_make_bootstrap_and_dsr_unavailable_without_zero_substitution():
    for returns in ([0.010], [0.010, -0.004]):
        result = analyze_return_series(
            returns,
            trade_count=2,
            min_observation_sessions=3,
            min_trade_count=2,
            bootstrap_iterations=50,
        )

        assert result["status"] == "unavailable"
        assert result["unavailable_reason"] == (
            "minimum_observation_sessions_not_met"
        )
        assert result["sample_requirements"]["passed"] is False
        assert result["observed"]["sessions"] == len(returns)
        assert result["observed"]["trades"] == 2
        assert result["observed"]["total_return"] is not None
        assert result["observed"]["annualized_return"] is None
        assert result["observed"]["annualized_sharpe"] is None
        assert result["bootstrap_confidence_interval"]["annualized_sharpe"] is None
        assert result["bootstrap_confidence_interval"]["total_return"] is None
        assert result["deflated_sharpe_ratio"]["computable"] is False
        assert result["deflated_sharpe_ratio"]["dsr"] is None
        assert result["deflated_sharpe_ratio"]["p_value"] is None
        assert result["deflated_sharpe_ratio"]["unavailable_reason"]

    measurable = analyze_return_series(
        [0.010, -0.004, 0.006],
        trade_count=2,
        min_observation_sessions=3,
        min_trade_count=2,
        bootstrap_iterations=50,
    )
    assert measurable["status"] == "available"
    assert measurable["sample_requirements"]["passed"] is True
    assert measurable["observed"]["annualized_return"] is not None
    assert measurable["observed"]["annualized_sharpe"] is not None
    assert measurable["bootstrap_confidence_interval"]["annualized_sharpe"] is not None
    assert measurable["deflated_sharpe_ratio"]["computable"] is True


def test_trial_ledger_counts_unique_recorded_trials_and_discards(tmp_path):
    database = tmp_path / "statistics.sqlite3"
    config_a = {"initial_cash": 100_000.0, "max_positions": 2}
    config_b = {"initial_cash": 100_000.0, "max_positions": 3}
    hash_a = trial_config_hash("candidate-a", config_a)
    hash_b = trial_config_hash("candidate-b", config_a)
    hash_c = trial_config_hash("candidate-b", config_b)
    with sqlite3.connect(str(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE backtest_runs (
                run_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL
            );
            CREATE TABLE research_evidence_runs (
                loop_id TEXT NOT NULL,
                candidate_signature TEXT NOT NULL,
                run_id TEXT NOT NULL
            );
            CREATE TABLE research_candidates (
                candidate_signature TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                holdout_status TEXT,
                superseded_by_loop_id TEXT
            );
            """
        )
        rows = [
            ("run-1", "candidate-a", config_a, hash_a),
            ("run-2", "candidate-a", config_a, hash_a),
            ("run-3", "candidate-b", config_a, hash_b),
            ("run-4", "candidate-b", config_b, hash_c),
        ]
        for run_id, candidate_id, config, config_hash in rows:
            connection.execute(
                "INSERT INTO backtest_runs VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    candidate_id,
                    canonical_json_bytes(config, trailing_newline=False).decode(),
                    config_hash,
                ),
            )
        connection.executemany(
            "INSERT INTO research_evidence_runs VALUES (?, ?, ?)",
            [
                ("loop-1", "signature-a", "run-1"),
                ("loop-1", "signature-b", "run-3"),
            ],
        )
        connection.execute(
            """
            INSERT INTO research_candidates (
                candidate_signature, stage, holdout_status, superseded_by_loop_id
            ) VALUES ('signature-b', 'rejected', NULL, NULL)
            """
        )

    ledger = build_trial_ledger(
        database,
        loop_id="loop-1",
        unrecorded_trials_upper_bound=2,
    )

    assert ledger["N_trials_total_recorded"] == 3
    assert ledger["N_trials_total"] == 5
    assert ledger["N_trials_this_loop"] == 2
    assert ledger["N_trials_discarded"] == 1
    assert ledger["ledger_complete"] is False
    assert ledger["ledger_accounting_complete"] is True
    assert ledger["N_trials_total_lower_bound"] == 3
    assert ledger["N_trials_total_upper_bound"] == 5
    assert ledger["recorded_trial_ids"] == sorted([hash_a, hash_b, hash_c])

    assumed = build_trial_ledger(
        database,
        loop_id="loop-1",
        assume_recorded_trials_complete=True,
    )
    assert assumed["ledger_complete"] is True
    assert assumed["ledger_accounting_complete"] is True
    assert assumed["N_trials_total_upper_bound"] == 3


def test_trial_ledger_without_exactness_or_finite_bound_is_not_acceptable(tmp_path):
    database = tmp_path / "unbounded-ledger.sqlite3"
    config = {"initial_cash": 100_000.0}
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            """
            CREATE TABLE backtest_runs (
                run_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO backtest_runs VALUES (?, ?, ?, ?)",
            (
                "run-1",
                "candidate-a",
                canonical_json_bytes(config, trailing_newline=False).decode(),
                trial_config_hash("candidate-a", config),
            ),
        )

    ledger = build_trial_ledger(database)

    assert ledger["N_trials_total"] == 1
    assert ledger["N_trials_total_lower_bound"] == 1
    assert ledger["N_trials_total_upper_bound"] is None
    assert ledger["ledger_complete"] is False
    assert ledger["ledger_accounting_complete"] is False
    assert ledger["ledger_basis"] == "recorded_evidence_lower_bound_only"


def test_run_statistics_persists_artifact_and_lineage(tmp_path):
    database = tmp_path / "run-statistics.sqlite3"
    config = {"initial_cash": 100_000.0, "max_positions": 2}
    config_hash = trial_config_hash("candidate-a", config)
    equity = []
    value = 100_000.0
    for index, daily_return in enumerate(_returns(), start=1):
        value *= 1.0 + daily_return
        equity.append(
            {
                "trade_date": f"2026-01-{index:02d}",
                "total_assets": value,
            }
        )
    with sqlite3.connect(str(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE backtest_runs (
                run_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                replay_id TEXT NOT NULL,
                code_version TEXT NOT NULL,
                status TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                initial_cash REAL NOT NULL,
                config_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                equity_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                config_hash TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO backtest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "experiment-1",
                "candidate-a",
                "replay-1",
                "test",
                "completed",
                "2026-01-01",
                "2026-01-20",
                100_000.0,
                canonical_json_bytes(config, trailing_newline=False).decode(),
                canonical_json_bytes({"total_return": 0.01}, trailing_newline=False).decode(),
                canonical_json_bytes(equity, trailing_newline=False).decode(),
                "2026-01-20T00:00:00Z",
                config_hash,
            ),
        )

    result = run_research_statistics(
        database,
        "run-1",
        bootstrap_iterations=100,
        output_path=tmp_path / "statistics.json",
        markdown_path=tmp_path / "statistics.md",
    )

    assert result["status"] == "unavailable"
    assert result["trial_ledger"]["N_trials_total"] == 1
    assert result["decision_status"] == "not_issued_phase_1b_required"
    assert result["phase_1b_ready"] is False
    with sqlite3.connect(str(database)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM research_statistics_runs"
        ).fetchone()[0]
    assert count == 1


def test_trial_schema_migration_backfills_config_hash(tmp_path):
    database = tmp_path / "legacy-trials.sqlite3"
    config = {"initial_cash": 100_000.0, "max_positions": 2}
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            """
            CREATE TABLE backtest_runs (
                run_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                replay_id TEXT NOT NULL,
                code_version TEXT NOT NULL,
                status TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                initial_cash REAL NOT NULL,
                config_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                equity_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO backtest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-old",
                "experiment-old",
                "candidate-old",
                "replay-old",
                "test",
                "completed",
                "2026-01-01",
                "2026-01-02",
                100_000.0,
                canonical_json_bytes(config, trailing_newline=False).decode(),
                "{}",
                "[]",
                "2026-01-02T00:00:00Z",
            ),
        )

    initialize_trial_backtest_schema(database)

    with sqlite3.connect(str(database)) as connection:
        config_hash = connection.execute(
            "SELECT config_hash FROM backtest_runs WHERE run_id='run-old'"
        ).fetchone()[0]
    assert config_hash == trial_config_hash("candidate-old", config)
