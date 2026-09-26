from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from kinace_v3.data_collection import (
    DailyBarRecord,
    ForwardLabelRecord,
    GateThresholds,
    RevisionStatus,
    RevisionStore,
    SecurityStateRecord,
    evaluate_gate,
    fill_provisional,
    ingest_records,
    run_gate,
)
from kinace_v3.data_collection.gate import candidate_evidence_summary
from kinace_v3.data_gate import run_data_gate
from kinace_v3.feature_snapshot import create_feature_snapshot, write_feature_snapshot
from kinace_v3.promotion import build_promotion_dry_run
from kinace_v3.search import SearchGateError, load_ready_data_gate, run_gated_search


ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, env: dict[str, str] | None = None):
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(ROOT / "src")
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "kinace_v3", *args],
        cwd=ROOT,
        env=process_env,
        check=False,
        capture_output=True,
        text=True,
    )


def _bar(decision_date: str, close: float = 10.5) -> DailyBarRecord:
    return DailyBarRecord(
        ts_code=f"{decision_date.replace('-', '')}.SZ",
        trade_date=decision_date,
        open=10.0,
        high=max(11.0, close),
        low=9.8,
        close=close,
        pre_close=9.9,
        vol=1000.0,
        amount=10000.0,
    )


def _dates(count: int) -> list[str]:
    first = date(2024, 1, 1)
    return [(first + timedelta(days=index)).isoformat() for index in range(count)]


def _quality_inputs(dates: list[str]) -> dict[str, dict]:
    return {
        decision_date: {
            "is_suspended": False,
            "is_limit_up": False,
            "is_limit_down": False,
            "labels": {"forward_return": 0.01},
        }
        for decision_date in dates
    }


def _add_candidate_trades(database: Path, count: int, *, bound: bool = True) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                run_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backtest_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                shares INTEGER,
                code TEXT,
                decision_date TEXT
            );
            CREATE TABLE IF NOT EXISTS research_audits (
                audit_id TEXT PRIMARY KEY,
                status TEXT,
                loop_id TEXT,
                holdout_run_id TEXT,
                model_candidate_id TEXT,
                candidate_signature TEXT,
                candidate_audit_passed INTEGER
            );
            CREATE TABLE IF NOT EXISTS research_loop_runs (
                loop_id TEXT PRIMARY KEY,
                holdout_status TEXT
            );
            CREATE TABLE IF NOT EXISTS research_evidence_runs (
                loop_id TEXT NOT NULL,
                candidate_signature TEXT NOT NULL,
                round_number INTEGER NOT NULL,
                run_kind TEXT NOT NULL,
                fold_number INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                PRIMARY KEY (loop_id, candidate_signature, run_kind, fold_number)
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO backtest_runs VALUES (?, ?)",
            ("run-1", "candidate-1"),
        )
        for index in range(count):
            connection.execute(
                "INSERT INTO backtest_trades "
                "(run_id, candidate_id, shares, code, decision_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "run-1",
                    "candidate-1",
                    100,
                    "000001",
                    f"2024-01-{index + 1:02d}",
                ),
            )
        connection.execute(
            "INSERT INTO backtest_trades "
            "(run_id, candidate_id, shares, code, decision_date) "
            "VALUES (?, ?, ?, ?, ?)",
            ("run-1", "candidate-1", 0, "000001", "2024-02-01"),
        )
        if bound:
            connection.execute(
                "INSERT OR REPLACE INTO research_audits "
                "(audit_id, status, loop_id, holdout_run_id, model_candidate_id, "
                "candidate_signature, candidate_audit_passed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "audit-1",
                    "passed",
                    "loop-1",
                    "run-1",
                    "candidate-1",
                    "candidate-signature",
                    1,
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO research_loop_runs "
                "(loop_id, holdout_status) VALUES (?, ?)",
                ("loop-1", "challenger_ready"),
            )
            connection.execute(
                "INSERT OR REPLACE INTO research_evidence_runs "
                "(loop_id, candidate_signature, round_number, run_kind, fold_number, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("loop-1", "candidate-signature", 0, "holdout", -1, "run-1"),
            )


def test_development_gate_allows_provisional_window_but_is_not_production(tmp_path):
    database = tmp_path / "v3.sqlite3"
    store = RevisionStore(database)
    fill_provisional(
        store,
        "daily_bars",
        [{"ts_code": "000001.SZ", "trade_date": "2024-01-02"}],
        baseline=10.0,
    )

    result = evaluate_gate(store, mode="development", max_decision_dates=1)

    assert result["status"] == "development_ready"
    assert result["ready"] is True
    assert result["development_only"] is True
    assert result["development_eligible"] is True
    assert result["release_eligible"] is False
    assert result["production_eligible"] is False
    assert result["production_ready"] is False
    assert result["counts"]["provisional_dates"] == 1
    assert result["failed_hard_gates"] == []


def test_release_gate_excludes_provisional_dates(tmp_path):
    database = tmp_path / "v3.sqlite3"
    store = RevisionStore(database)
    fill_provisional(
        store,
        "daily_bars",
        [{"ts_code": "000001.SZ", "trade_date": "2024-01-02"}],
        baseline=10.0,
    )

    result = evaluate_gate(store, mode="release")

    assert result["ready"] is False
    assert result["production_eligible"] is False
    assert result["counts"]["data_approved_decision_dates"] == 0
    assert result["counts"]["provisional_dates"] == 1
    assert "data_approved_decision_dates" in result["failed_hard_gates"]
    assert "no_provisional_dates" in result["failed_hard_gates"]


def test_release_gate_passes_only_with_dates_pit_labels_and_candidate_trades(tmp_path):
    database = tmp_path / "v3.sqlite3"
    store = RevisionStore(database)
    decision_dates = _dates(372)
    ingest_records(
        store,
        "daily_bars",
        [_bar(decision_date) for decision_date in decision_dates],
        status=RevisionStatus.OBSERVED,
        source="fixture",
    )
    _add_candidate_trades(database, 30)

    result = evaluate_gate(
        store,
        mode="release",
        quality_inputs=_quality_inputs(decision_dates),
    )

    assert result["status"] == "release_ready"
    assert result["ready"] is True
    assert result["development_only"] is False
    assert result["release_eligible"] is True
    assert result["production_eligible"] is True
    assert result["counts"]["data_approved_decision_dates"] == 372
    assert result["counts"]["nonzero_candidate_specific_trades"] == 30
    assert result["failed_hard_gates"] == []


def test_release_gate_rejects_unbound_raw_candidate_trades(tmp_path):
    database = tmp_path / "unbound.sqlite3"
    store = RevisionStore(database)
    decision_dates = _dates(372)
    ingest_records(
        store,
        "daily_bars",
        [_bar(decision_date) for decision_date in decision_dates],
        status=RevisionStatus.OBSERVED,
        source="fixture",
    )
    _add_candidate_trades(database, 30, bound=False)

    result = evaluate_gate(
        store,
        mode="release",
        quality_inputs=_quality_inputs(decision_dates),
    )

    assert result["ready"] is False
    assert result["counts"]["raw_nonzero_candidate_specific_trades"] == 30
    assert result["counts"]["bound_candidate_specific_trades"] == 0
    assert "nonzero_candidate_specific_trades" in result["failed_hard_gates"]


def test_candidate_evidence_summary_can_scope_to_one_loop_candidate(tmp_path):
    database = tmp_path / "scoped.sqlite3"
    _add_candidate_trades(database, 5)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO backtest_runs VALUES (?, ?)",
            ("run-2", "candidate-2"),
        )
        for index in range(7):
            connection.execute(
                "INSERT INTO backtest_trades "
                "(run_id, candidate_id, shares, code, decision_date) "
                "VALUES (?, ?, ?, ?, ?)",
                ("run-2", "candidate-2", 100, "000002", f"2024-03-{index + 1:02d}"),
            )
        connection.execute(
            "INSERT INTO research_audits "
            "(audit_id, status, loop_id, holdout_run_id, model_candidate_id, "
            "candidate_signature, candidate_audit_passed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "audit-2",
                "passed",
                "loop-2",
                "run-2",
                "candidate-2",
                "candidate-signature-2",
                1,
            ),
        )
        connection.execute(
            "INSERT INTO research_loop_runs (loop_id, holdout_status) VALUES (?, ?)",
            ("loop-2", "challenger_ready"),
        )
        connection.execute(
            "INSERT INTO research_evidence_runs "
            "(loop_id, candidate_signature, round_number, run_kind, fold_number, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("loop-2", "candidate-signature-2", 0, "holdout", -1, "run-2"),
        )

    all_evidence = candidate_evidence_summary(database)
    scoped = candidate_evidence_summary(
        database,
        loop_id="loop-1",
        candidate_signature="candidate-signature",
    )

    assert all_evidence["bound_trades"] == 12
    assert all_evidence["distinct_candidates"] == 2
    assert scoped["bound_trades"] == 5
    assert scoped["distinct_candidates"] == 1


def test_release_gate_joins_security_state_and_forward_label_datasets(tmp_path):
    database = tmp_path / "joined.sqlite3"
    store = RevisionStore(database)
    dates = _dates(3)
    bars = [_bar(decision_date) for decision_date in dates]
    states = [
        SecurityStateRecord(
            ts_code=bar.ts_code,
            trade_date=bar.trade_date,
            is_suspended=False,
            is_limit_up=False,
            is_limit_down=False,
            prev_close=bar.pre_close,
        )
        for bar in bars
    ]
    labels = [
        ForwardLabelRecord(
            ts_code=current.ts_code,
            decision_date=current.trade_date,
            next_trade_date=following.trade_date,
            next_open=following.open,
            next_close=following.close,
        )
        for current, following in zip(bars, bars[1:])
    ]
    ingest_records(store, "daily_bars", bars, status=RevisionStatus.OBSERVED, source="fixture")
    ingest_records(store, "security_state", states, status=RevisionStatus.OBSERVED, source="fixture")
    ingest_records(store, "forward_labels", labels, status=RevisionStatus.OBSERVED, source="fixture")

    result = evaluate_gate(
        store,
        mode="release",
        thresholds=GateThresholds(
            release_min_decision_dates=3,
            release_min_nonzero_candidate_trades=0,
            release_min_independent_trade_dates=0,
        ),
    )

    assert result["ready"] is True
    assert result["status"] == "release_ready"
    assert result["quality"]["state_inputs_ready"] is True
    assert result["quality"]["labels_ready"] is True
    assert result["counts"]["security_state_records"] == 3
    assert result["counts"]["forward_label_records"] == 2


@pytest.mark.parametrize(
    ("date_count", "trade_count", "quality_inputs", "provisional", "failed_gate"),
    [
        (371, 30, True, False, "data_approved_decision_dates"),
        (372, 29, True, False, "nonzero_candidate_specific_trades"),
        (372, 30, False, False, "label_readiness"),
        (372, 30, True, True, "no_provisional_dates"),
    ],
)
def test_release_threshold_failures_are_explicit(
    tmp_path,
    date_count,
    trade_count,
    quality_inputs,
    provisional,
    failed_gate,
):
    database = tmp_path / "v3.sqlite3"
    store = RevisionStore(database)
    decision_dates = _dates(date_count)
    ingest_records(
        store,
        "daily_bars",
        [_bar(decision_date) for decision_date in decision_dates],
        status=RevisionStatus.OBSERVED,
        source="fixture",
    )
    if provisional:
        fill_provisional(
            store,
            "daily_bars",
            [{"ts_code": "999999.SZ", "trade_date": "2024-12-31"}],
            baseline=10.0,
        )
    _add_candidate_trades(database, trade_count)

    result = evaluate_gate(
        store,
        mode="release",
        quality_inputs=_quality_inputs(decision_dates) if quality_inputs else None,
    )

    assert result["ready"] is False
    assert failed_gate in result["failed_hard_gates"]


def test_gate_mode_artifacts_preserve_legacy_data_gate_path_compatibility(tmp_path):
    database = tmp_path / "v3.sqlite3"
    RevisionStore(database)
    development_path = tmp_path / "data_gate.development.json"
    release_path = tmp_path / "data_gate.release.json"

    development = run_gate(
        database,
        mode="development",
        output_path=development_path,
    )
    release = run_gate(database, mode="release", output_path=release_path)

    assert development["output_path"] == str(development_path)
    assert release["output_path"] == str(release_path)
    assert json.loads(development_path.read_text(encoding="utf-8"))["mode"] == "development"
    assert json.loads(release_path.read_text(encoding="utf-8"))["mode"] == "release"
    wrapped = run_data_gate(
        output_path=tmp_path / "wrapped.json",
        mode="release",
        database=database,
    )
    assert wrapped["artifact_type"] == "kinace_v3_data_collection_gate"


def test_data_gate_cli_writes_mode_specific_artifacts(tmp_path):
    database = tmp_path / "v3.sqlite3"
    store = RevisionStore(database)
    fill_provisional(
        store,
        "daily_bars",
        [{"ts_code": "000001.SZ", "trade_date": "2024-01-02"}],
        baseline=10.0,
    )
    development_path = tmp_path / "development.json"
    release_path = tmp_path / "release.json"

    development = _run_cli(
        "data-gate",
        "--mode",
        "development",
        "--database",
        str(database),
        "--output",
        str(development_path),
        "--json",
    )
    release = _run_cli(
        "data-gate",
        "--mode",
        "release",
        "--database",
        str(database),
        "--output",
        str(release_path),
        "--json",
    )

    assert development.returncode == 0, development.stderr
    assert release.returncode == 0, release.stderr
    assert json.loads(development.stdout)["mode"] == "development"
    assert json.loads(release.stdout)["mode"] == "release"
    assert development_path.is_file()
    assert release_path.is_file()


def _write_ready_development_gate(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "artifact_type": "kinace_v3_data_collection_gate",
                "mode": "development",
                "status": "development_ready",
                "ready": True,
                "development_only": True,
                "development_eligible": True,
                "release_eligible": False,
                "production_eligible": False,
                "failed_hard_gates": [],
            }
        ),
        encoding="utf-8",
    )


def _model_config() -> dict:
    return {
        "version": "development-search",
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


def test_search_requires_release_by_default_and_development_is_explicit(tmp_path):
    development_gate = tmp_path / "development.json"
    _write_ready_development_gate(development_gate)

    with pytest.raises(SearchGateError, match="mode=development"):
        load_ready_data_gate(development_gate)
    assert load_ready_data_gate(development_gate, development=True)["development_only"] is True

    model = _model_config()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = create_feature_snapshot(
        decision_date="2024-01-02",
        candidates=[
            {
                "code": "000001",
                "raw_inputs": {
                    "rel_sector_strength": 0.2,
                    "close_location_5": 0.8,
                    "volume_ratio": 1.2,
                    "log_amount": 6.5,
                    "turnover": 0.4,
                    "proximity_20d_high": 0.9,
                    "ret_5": 0.03,
                    "gap": 0.01,
                },
                "penalty_inputs": {
                    "rsi": 55.0,
                    "kdj_j": 70.0,
                    "amplitude_5d": 0.02,
                },
                "labels": {"forward_return": 0.02},
            }
        ],
        model_config=model,
        data_gate={"status": "development_ready", "ready": True},
    )
    write_feature_snapshot(snapshot, snapshot_path)
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps([model]), encoding="utf-8")

    result = run_gated_search(
        data_gate_path=development_gate,
        snapshot_path=snapshot_path,
        candidates_path=candidates_path,
        output_path=tmp_path / "search.json",
        database=tmp_path / "search.sqlite3",
        development=True,
    )

    assert result["development_only"] is True
    assert result["production_ready"] is False
    assert result["production_eligible"] is False
    assert result["production_changed"] is False


def test_development_gate_cannot_authorize_production_promotion(tmp_path):
    development_gate = tmp_path / "development.json"
    _write_ready_development_gate(development_gate)

    result = build_promotion_dry_run(
        database=tmp_path / "v3.sqlite3",
        data_gate_path=development_gate,
    )

    assert result["status"] == "blocked_data_gate"
    assert result["production_changed"] is False
    assert result["mutation_allowed"] is False
