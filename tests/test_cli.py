import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _env(**updates):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update(updates)
    return env


def _run(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "kinace_v3", *args],
        cwd=ROOT,
        env=env or _env(),
        check=False,
        capture_output=True,
        text=True,
    )


def test_help_exposes_required_commands():
    result = _run("--help")

    assert result.returncode == 0, result.stderr
    for command in (
        "doctor",
        "db-init",
        "sandbox-init",
        "sandbox-verify",
        "evidence-freeze",
        "evidence-verify",
        "data-gate",
        "data-backfill",
        "history-bootstrap",
        "market-sync",
        "price-rebuild",
        "universe-bootstrap",
        "history-snapshots",
        "pit-audit",
        "data-fill-provisional",
        "data-revise",
        "data-quality",
        "data-status",
        "snapshot",
        "replay",
        "search",
        "train",
        "backtest",
        "portfolio-backtest",
        "daily-run",
        "optimize",
        "research-loop",
        "research-diagnose",
        "research-audit",
        "research-stability",
        "research-holdout-extension",
        "research-statistics",
        "research-decision",
        "research-label-alignment",
        "research-standardization",
        "research-objective",
        "research-weighting",
        "research-weight-audit",
        "research-stress",
        "research-qualify",
        "report",
        "model-promote",
    ):
        assert command in result.stdout


def test_version_reports_v35_core():
    result = _run("--version")

    assert result.returncode == 0, result.stderr
    assert "3.5.0" in result.stdout


def test_db_init_uses_only_v3_override(tmp_path):
    database = tmp_path / "v3.sqlite3"
    env = _env(
        KINACE_V32_DB_PATH=str(database),
        KINACE_V2_DB_PATH=r"D:\kinacesql\kinace_v2.sqlite3",
    )

    result = _run("db-init", env=env)

    assert result.returncode == 0, result.stderr
    assert database.is_file()
    with sqlite3.connect(str(database)) as connection:
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
    assert row == ("3",)


def test_history_snapshots_cli_fails_closed_without_a_pit_policy(tmp_path):
    output_dir = tmp_path / "snapshots"
    result = _run(
        "history-snapshots",
        "--database",
        str(tmp_path / "v3.sqlite3"),
        "--output-dir",
        str(output_dir),
        "--pit-policy",
        str(tmp_path / "missing-policy.json"),
        "--json",
    )

    assert result.returncode == 2
    assert "historical snapshot construction requires" in result.stderr
    assert not output_dir.exists()


def test_sandbox_init_and_verify_use_only_the_explicit_root(tmp_path):
    sandbox_root = tmp_path / "sandbox"
    protected_root = tmp_path / "protected"
    formal_root = tmp_path / "formal"
    formal_args = [
        "--formal-database",
        str(formal_root / "formal.sqlite3"),
        "--formal-snapshots",
        str(formal_root / "snapshots"),
        "--formal-trial-ledger",
        str(formal_root / "trial-ledger.jsonl"),
        "--formal-reports",
        str(formal_root / "reports"),
        "--formal-holdout-registry",
        str(formal_root / "holdout-registry.json"),
        "--protected-database-root",
        str(protected_root),
    ]

    initialized = _run(
        "sandbox-init",
        "--root",
        str(sandbox_root),
        *formal_args,
        "--json",
    )
    assert initialized.returncode == 0, initialized.stderr
    init_payload = json.loads(initialized.stdout)
    assert init_payload["track"] == "sandbox"
    assert Path(init_payload["database"]).is_relative_to(sandbox_root)

    verification_path = tmp_path / "verification.json"
    verified = _run(
        "sandbox-verify",
        "--root",
        str(sandbox_root),
        *formal_args,
        "--output",
        str(verification_path),
        "--json",
    )
    assert verified.returncode == 0, verified.stderr
    verify_payload = json.loads(verified.stdout)
    assert verify_payload["status"] == "passed"
    assert verify_payload["track"] == "sandbox"
    assert verification_path.is_file()
    assert not formal_root.exists()


def test_doctor_reports_offline_and_isolated():
    result = _run("doctor")

    assert result.returncode == 0, result.stderr
    assert "package import isolation: ok" in result.stdout
    assert "offline operation: ok" in result.stdout
    assert "database: D:\\kinacev3.3\\data\\kinacesqlv3.2\\kinace_v3_2.sqlite3" in result.stdout


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _model_config(version: str = "synthetic-cli") -> dict:
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


def test_snapshot_replay_backtest_and_report_commands_work_together(tmp_path):
    database = tmp_path / "v3.sqlite3"
    model = _model_config()
    snapshot_input = _write_json(
        tmp_path / "snapshot-input.json",
        {
            "decision_date": "2024-01-02",
            "model_config": model,
            "candidates": [
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
        },
    )
    candidate_config = _write_json(tmp_path / "candidate.json", model)
    bars = _write_json(
        tmp_path / "bars.json",
        {
            "000001": [
                {"trade_date": "2024-01-01", "open": 90, "high": 91, "low": 89, "close": 90, "volume": 1000, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
                {"trade_date": "2024-01-02", "open": 95, "high": 106, "low": 94, "close": 105, "volume": 1000, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
                {"trade_date": "2024-01-03", "open": 101, "high": 108, "low": 100, "close": 104, "volume": 1000, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
                {"trade_date": "2024-01-04", "open": 104, "high": 106, "low": 102, "close": 105, "volume": 1000, "is_suspended": False, "is_limit_up": False, "is_limit_down": False},
            ]
        },
    )
    backtest_config = _write_json(
        tmp_path / "backtest.json",
        {
            "initial_cash": 100000.0,
            "position_size_pct": 0.5,
            "lot_size": 1,
            "max_hold_sessions": 2,
            "slippage_bps": 0.0,
        },
    )

    snapshot_result = _run(
        "snapshot",
        "--input",
        str(snapshot_input),
        "--output",
        str(tmp_path / "snapshot.json"),
        "--database",
        str(database),
        "--json",
    )
    assert snapshot_result.returncode == 0, snapshot_result.stderr
    snapshot = json.loads(snapshot_result.stdout)

    replay_result = _run(
        "replay",
        "--snapshot",
        snapshot["artifact_path"],
        "--candidate-config",
        str(candidate_config),
        "--output",
        str(tmp_path / "replay.json"),
        "--database",
        str(database),
        "--json",
    )
    assert replay_result.returncode == 0, replay_result.stderr
    replay = json.loads(replay_result.stdout)

    backtest_result = _run(
        "backtest",
        "--replay-id",
        replay["replay_id"],
        "--candidate-id",
        replay["candidate_id"],
        "--experiment-id",
        replay["experiment_id"],
        "--bars",
        str(bars),
        "--config-file",
        str(backtest_config),
        "--database",
        str(database),
        "--json",
    )
    assert backtest_result.returncode == 0, backtest_result.stderr
    backtest = json.loads(backtest_result.stdout)
    assert backtest["metrics"]["trade_count"] == 1

    report_result = _run(
        "report",
        "--database",
        str(database),
        "--run-id",
        backtest["run_id"],
        "--output",
        str(tmp_path / "report.md"),
        "--json",
    )
    assert report_result.returncode == 0, report_result.stderr
    report = json.loads(report_result.stdout)
    assert report["counts"]["backtest_runs"] == 1
    assert report["runs"][0]["run_id"] == backtest["run_id"]
    assert Path(report["output_path"]).is_file()


def test_search_and_train_fail_closed_for_blocked_or_missing_gate(tmp_path):
    blocked_gate = _write_json(
        tmp_path / "blocked.json",
        {
            "status": "blocked_insufficient_point_in_time_data",
            "ready": False,
            "failed_hard_gates": ["independent_decision_dates"],
        },
    )

    for command in ("search", "train"):
        result = _run(command, "--data-gate", str(blocked_gate), "--json")
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["status"] == "blocked"
        assert payload["search_started"] is False

    missing = _run("search", "--data-gate", str(tmp_path / "missing.json"), "--json")
    assert missing.returncode == 2
    assert "missing" in json.loads(missing.stdout)["error"]


def test_search_runs_only_after_a_ready_gate(tmp_path):
    model = _model_config("ready-search")
    snapshot_input = _write_json(
        tmp_path / "ready-input.json",
        {
            "decision_date": "2024-01-02",
            "model_config": model,
            "candidates": [
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
        },
    )
    snapshot = _run(
        "snapshot",
        "--input",
        str(snapshot_input),
        "--output",
        str(tmp_path / "ready-snapshot.json"),
        "--no-persist",
        "--json",
    )
    assert snapshot.returncode == 0, snapshot.stderr
    snapshot_payload = json.loads(snapshot.stdout)
    gate = _write_json(
        tmp_path / "ready-gate.json",
        {"status": "ready", "ready": True, "failed_hard_gates": []},
    )
    candidates = _write_json(tmp_path / "candidates.json", [model])
    database = tmp_path / "search.sqlite3"

    result = _run(
        "search",
        "--data-gate",
        str(gate),
        "--snapshot",
        snapshot_payload["artifact_path"],
        "--candidates",
        str(candidates),
        "--database",
        str(database),
        "--output",
        str(tmp_path / "search.json"),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "research_only"
    assert payload["candidate_count"] == 1
    assert payload["production_ready"] is False
    with sqlite3.connect(str(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1


def test_model_promote_is_non_mutating_and_requires_dry_run(tmp_path):
    database = tmp_path / "v3.sqlite3"
    blocked_gate = _write_json(
        tmp_path / "blocked.json",
        {"status": "blocked", "ready": False, "failed_hard_gates": ["data"]},
    )
    production_path = ROOT / "config" / "model.production.v3.json"
    before = production_path.read_bytes() if production_path.exists() else None

    denied = _run("model-promote", "--database", str(database))
    assert denied.returncode == 2
    assert "dry-run only" in denied.stderr

    audit = _run(
        "model-promote",
        "--dry-run",
        "--database",
        str(database),
        "--data-gate",
        str(blocked_gate),
        "--json",
    )
    assert audit.returncode == 0, audit.stderr
    payload = json.loads(audit.stdout)
    assert payload["dry_run"] is True
    assert payload["production_changed"] is False
    assert payload["mutation_allowed"] is False
    assert Path(payload["production_path"]).is_relative_to(ROOT / "config")
    after = production_path.read_bytes() if production_path.exists() else None
    assert after == before

    outside = _run(
        "model-promote",
        "--dry-run",
        "--database",
        str(database),
        "--production-path",
        str(tmp_path / "outside.json"),
    )
    assert outside.returncode == 2
    assert "must stay under" in outside.stderr
