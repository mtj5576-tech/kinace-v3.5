from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from kinace_v3.data_collection.ingest import ingest_records
from kinace_v3.data_collection.models import DailyBarRecord, RevisionStatus
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.schedule_runner import (
    _ensure_schema,
    _backup_database,
    _execute_holdout_extension,
    _execute_postclose,
    _execute_weekly,
    _reconcile_stale_runs,
    _send_alert_webhook,
    CHINA_TZ,
    restore_database,
    delivery_status,
    execute_mode,
)
from kinace_v3.research_holdout import register_holdout_extension_freeze


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], tuple[int, ...]]] = []

    def __call__(self, arguments, *, log_path, allowed_exit_codes=(0,)):
        self.calls.append((list(arguments), tuple(allowed_exit_codes)))
        return 0


def _seed_daily_bar(database: Path) -> None:
    store = RevisionStore(database)
    ingest_records(
        store,
        "daily_bars",
        [
            DailyBarRecord(
                ts_code="000001",
                trade_date="2026-09-15",
                open=10.0,
                high=10.2,
                low=9.9,
                close=10.1,
                vol=100000,
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="v2-history",
    )


def _seed_daily_run(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_runs (
                run_id TEXT PRIMARY KEY,
                decision_date TEXT,
                status TEXT,
                completed_at_utc TEXT,
                snapshot_id TEXT,
                replay_id TEXT,
                backtest_run_id TEXT,
                manifest_path TEXT,
                report_path TEXT
            )
            """
        )


def _seed_postclose_ledger(database: Path) -> None:
    from kinace_v3.schedule_runner import _ensure_schema

    _ensure_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO scheduler_runs (
                run_id, mode, scheduled_at_utc, started_at_utc,
                completed_at_utc, status, decision_date, command_json, log_path
            ) VALUES (
                'postclose-1','postclose','2026-09-15T11:00:00Z',
                '2026-09-15T11:00:00Z','2026-09-15T11:05:00Z',
                'completed','2026-09-15','{}','test.log'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO daily_runs (
                run_id, decision_date, status, completed_at_utc,
                snapshot_id, replay_id, backtest_run_id, manifest_path, report_path
            ) VALUES (
                'daily-1','2026-09-15','completed','2026-09-15T11:00:00Z',
                'snapshot-1','replay-1','portfolio-1',
                'manifest.json','report.md'
            )
            """
        )


def test_preopen_execution_records_scheduler_ledger(tmp_path):
    database = tmp_path / "v32.sqlite3"
    _seed_daily_bar(database)
    _seed_daily_run(database)
    _seed_postclose_ledger(database)
    runner = RecordingRunner()

    result = execute_mode(
        "preopen",
        database=database,
        report_dir=tmp_path / "reports",
        runner=runner,
    )

    assert result["status"] == "completed"
    assert result["decision_date"] == "2026-09-15"
    assert runner.calls[0][0][:4] == [
        "daily-run",
        "--run-purpose",
        "preopen",
        "--decision-date",
    ]
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT mode, status, decision_date FROM scheduler_runs "
            "WHERE mode='preopen'"
        ).fetchone()
    assert row == ("preopen", "completed", "2026-09-15")


def test_postclose_syncs_every_v3_metadata_code(tmp_path):
    database = tmp_path / "v32.sqlite3"
    _seed_daily_bar(database)
    runner = RecordingRunner()

    status, decision_date = _execute_postclose(
        database=database,
        project_root=tmp_path / "project",
        log_path=tmp_path / "postclose.log",
        runner=runner,
    )

    assert status == "completed"
    assert decision_date == "2026-09-15"
    assert runner.calls[0][0][0] == "market-sync"
    assert "--all-metadata" in runner.calls[0][0]
    assert runner.calls[1][0][0] == "daily-run"


def test_postclose_force_reruns_existing_decision(tmp_path):
    database = tmp_path / "v32.sqlite3"
    _seed_daily_bar(database)
    _seed_daily_run(database)
    _seed_postclose_ledger(database)
    runner = RecordingRunner()

    status, decision_date = _execute_postclose(
        database=database,
        project_root=tmp_path / "project",
        log_path=tmp_path / "postclose.log",
        runner=runner,
        force=True,
    )

    assert status == "completed"
    assert decision_date == "2026-09-15"
    assert [call[0][0] for call in runner.calls] == ["market-sync", "daily-run"]
    assert "--rebase-price-basis" in runner.calls[0][0]


def test_postclose_latest_only_checks_only_today(tmp_path):
    database = tmp_path / "v32.sqlite3"
    _seed_daily_bar(database)
    _seed_daily_run(database)
    _seed_postclose_ledger(database)
    runner = RecordingRunner()

    status, decision_date = _execute_postclose(
        database=database,
        project_root=tmp_path / "project",
        log_path=tmp_path / "postclose.log",
        runner=runner,
        latest_only=True,
    )

    assert status == "no_new_data"
    assert decision_date == "2026-09-15"
    assert len(runner.calls) == 1
    arguments = runner.calls[0][0]
    assert "--latest-only" in arguments
    start = arguments[arguments.index("--start-date") + 1]
    end = arguments[arguments.index("--end-date") + 1]
    assert start == datetime.now(CHINA_TZ).date().isoformat()
    assert end == start


def test_execute_mode_can_skip_backup_for_fast_refresh(tmp_path, monkeypatch):
    database = tmp_path / "v32.sqlite3"
    _seed_daily_bar(database)
    _seed_daily_run(database)
    _seed_postclose_ledger(database)
    runner = RecordingRunner()

    def fail_backup(*args, **kwargs):
        raise AssertionError("fast refresh must not back up the full database")

    monkeypatch.setattr("kinace_v3.schedule_runner._backup_database", fail_backup)
    result = execute_mode(
        "postclose",
        database=database,
        report_dir=tmp_path / "reports",
        runner=runner,
        latest_only=True,
        backup=False,
    )

    assert result["status"] == "no_new_data"


def test_delivery_status_counts_distinct_successful_trading_days(tmp_path, monkeypatch):
    database = tmp_path / "v32.sqlite3"
    _seed_daily_bar(database)
    from kinace_v3.schedule_runner import _ensure_schema

    monkeypatch.setenv("KINACE_ALERT_WEBHOOK_URL", "https://alerts.example.test/hook")
    _ensure_schema(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO scheduler_runs (
                run_id, mode, scheduled_at_utc, started_at_utc,
                completed_at_utc, status, decision_date, command_json, log_path
            ) VALUES (?, 'postclose', ?, ?, ?, 'completed', ?, '{}', 'test.log')
            """,
            [
                (
                    f"run-{index}",
                    f"2026-08-{index + 1:02d}T11:00:00Z",
                    f"2026-08-{index + 1:02d}T11:00:00Z",
                    f"2026-08-{index + 1:02d}T11:05:00Z",
                    f"2026-08-{index + 1:02d}",
                )
                for index in range(20)
            ],
        )

    result = delivery_status(database=database, report_dir=tmp_path / "reports")

    assert result["unattended_trading_days"] == 20
    assert result["ready"] is False
    assert result["alert_webhook"] == {
        "configured": True,
        "host": "alerts.example.test",
    }


def test_stale_running_and_old_failures_are_reconciled(tmp_path):
    database = tmp_path / "v32.sqlite3"
    _ensure_schema(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO scheduler_runs (
                run_id, mode, scheduled_at_utc, started_at_utc,
                completed_at_utc, status, decision_date, command_json, log_path
            ) VALUES (?, 'postclose', ?, ?, ?, ?, ?, '{}', 'test.log')
            """,
            [
                (
                    "failed-old",
                    "2026-09-16T10:00:00Z",
                    "2026-09-16T10:00:00Z",
                    "2026-09-16T10:00:01Z",
                    "failed",
                    None,
                ),
                (
                    "stale-running",
                    "2026-09-16T11:00:00Z",
                    "2026-09-16T11:00:00Z",
                    None,
                    "running",
                    None,
                ),
                (
                    "success",
                    "2026-09-16T12:00:00Z",
                    "2026-09-16T12:00:00Z",
                    "2026-09-16T12:05:00Z",
                    "no_new_data",
                    "2026-09-16",
                ),
            ],
        )

    reconciled = _reconcile_stale_runs(database, tmp_path / "project")
    result = delivery_status(
        database=database,
        report_dir=tmp_path / "project" / "reports" / "scheduler",
    )

    assert reconciled == ["stale-running"]
    assert result["active_scheduler_runs"] == []
    assert result["unresolved_failed_runs"] == []
    assert len(result["resolved_failed_runs"]) == 2


def test_weekly_runs_preflight_loop_then_candidate_audit(tmp_path):
    project = tmp_path / "project"
    reports = project / "reports"
    reports.mkdir(parents=True)
    calls: list[list[str]] = []

    def runner(arguments, *, log_path, allowed_exit_codes=(0,)):
        args = list(arguments)
        calls.append(args)
        if args[0] == "research-audit" and "--preflight" in args:
            output = Path(args[args.index("--output") + 1])
            output.write_text(json.dumps({"research_eligible": True}), encoding="utf-8")
        elif args[0] == "research-loop":
            output = Path(args[args.index("--output") + 1])
            candidate = reports / "new-holdout.json"
            candidate.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "snapshot_manifest": {"snapshot_set_id": "snapshot-set-1"},
                    }
                ),
                encoding="utf-8",
            )
            output.write_text(
                json.dumps(
                    {
                        "loop_id": "loop-1",
                        "status": "challenger_ready",
                        "holdout": {
                            "portfolio_output_path": str(candidate),
                            "portfolio_run_id": "run-1",
                        },
                    }
                ),
                encoding="utf-8",
            )
        elif args[0] == "research-stability":
            output = Path(args[args.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "loop_id": "loop-1",
                        "status": "review_required",
                    }
                ),
                encoding="utf-8",
            )
        elif args[0] == "research-audit":
            output = Path(args[args.index("--output") + 1])
            output.write_text(
                json.dumps({"candidate_audit_passed": True}), encoding="utf-8"
            )
        return 0

    status, loop_id = _execute_weekly(
        database=tmp_path / "v32.sqlite3",
        project_root=project,
        log_path=tmp_path / "weekly.log",
        runner=runner,
    )

    assert status == "completed"
    assert loop_id == "loop-1"
    assert [call[0] for call in calls] == [
        "research-audit",
        "research-loop",
        "research-stability",
        "research-audit",
    ]
    assert "--preflight" in calls[0]
    assert "--loop-report" in calls[3]
    assert "--bind-loop" in calls[3]


def test_holdout_extension_scheduler_uses_pending_freeze(tmp_path):
    project = tmp_path / "project"
    reports = project / "reports"
    reports.mkdir(parents=True)
    freeze = project / "freeze.json"
    freeze.write_text("{}", encoding="utf-8")
    (reports / "research_loop_scheduled.json").write_text(
        json.dumps(
            {
                "holdout_status": "holdout_extension_pending",
                "holdout": {"freeze_path": str(freeze)},
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(arguments, *, log_path, allowed_exit_codes=(0,)):
        args = list(arguments)
        calls.append(args)
        output = Path(args[args.index("--output") + 1])
        output.write_text(
            json.dumps({"status": "awaiting_data", "extension_id": "extension-1"}),
            encoding="utf-8",
        )
        return 2

    status, extension_id = _execute_holdout_extension(
        database=tmp_path / "v32.sqlite3",
        project_root=project,
        log_path=tmp_path / "extension.log",
        runner=runner,
    )

    assert status == "awaiting_data"
    assert extension_id == "extension-1"
    assert calls[0][0] == "research-holdout-extension"
    assert calls[0][calls[0].index("--freeze") + 1] == str(freeze)


def test_weekly_prefers_pending_extension_before_new_research(tmp_path):
    project = tmp_path / "project"
    reports = project / "reports"
    reports.mkdir(parents=True)
    database = tmp_path / "v32.sqlite3"
    freeze_path = project / "freeze.json"
    freeze_path.write_text("{}", encoding="utf-8")
    register_holdout_extension_freeze(
        {
            "freeze_id": "freeze-1",
            "candidate_signature": "candidate-1",
            "evaluation_protocol_hash": "protocol-1",
            "policy_id": "policy-1",
        },
        database=database,
        freeze_path=freeze_path,
        registry_path=reports / "holdout_extension_registry.json",
    )
    calls: list[list[str]] = []

    def runner(arguments, *, log_path, allowed_exit_codes=(0,)):
        args = list(arguments)
        calls.append(args)
        output = Path(args[args.index("--output") + 1])
        output.write_text(
            json.dumps({"status": "awaiting_data", "extension_id": "extension-1"}),
            encoding="utf-8",
        )
        return 2

    status, extension_id = _execute_weekly(
        database=database,
        project_root=project,
        log_path=tmp_path / "weekly.log",
        runner=runner,
    )

    assert status == "awaiting_data"
    assert extension_id == "extension-1"
    assert [call[0] for call in calls] == ["research-holdout-extension"]


def test_alert_webhook_reports_configuration_and_success(tmp_path, monkeypatch):
    alert = tmp_path / "alert.json"
    monkeypatch.delenv("KINACE_ALERT_WEBHOOK_URL", raising=False)
    assert _send_alert_webhook(mode="postclose", error="boom", alert_path=alert)[
        "status"
    ] == "not_configured"

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(request, timeout):
        assert request.method == "POST"
        assert timeout == 10
        return Response()

    monkeypatch.setenv("KINACE_ALERT_WEBHOOK_URL", "https://alerts.example.test/hook")
    monkeypatch.setattr("kinace_v3.schedule_runner.urllib.request.urlopen", fake_urlopen)
    result = _send_alert_webhook(mode="postclose", error="boom", alert_path=alert)
    assert result["status"] == "sent"
    assert result["http_status"] == 204


def test_database_backup_and_restore_verify_integrity(tmp_path):
    database = tmp_path / "v32.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('original')")
        connection.commit()
    finally:
        connection.close()

    backup = _backup_database(database, tmp_path / "backups")
    assert backup.is_file()
    assert Path(str(backup) + ".sha256").is_file()

    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE marker SET value='changed'")
        connection.commit()
    finally:
        connection.close()

    restored = restore_database(backup, database)
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "original"
