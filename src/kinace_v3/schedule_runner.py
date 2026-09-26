"""Unattended Kinace V3.2 scheduler orchestration and delivery ledger."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from .config import DEFAULT_DATABASE_PATH, PROJECT_ROOT
from .data_collection.store import RevisionStore
from .research_holdout import (
    DEFAULT_EXTENSION_OUTPUT_DIR,
    DEFAULT_EXTENSION_REPORT_DIR,
    pending_holdout_extension_records,
    update_holdout_extension_registry,
)


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
UTC = timezone.utc
KINACE_DATA_ROOT = Path(r"D:\kinacesqlv3.2")
CANONICAL_SNAPSHOT_DIR = KINACE_DATA_ROOT / "snapshots_canonical"
PARITY_PATH = KINACE_DATA_ROOT / "parity" / "latest.json"
SUCCESS_STATUSES = {"completed", "no_new_data"}
SCHEDULE_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL,
    decision_date TEXT,
    command_json TEXT NOT NULL,
    log_path TEXT NOT NULL,
    error_text TEXT
)
"""
SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_pid(path: Path) -> int | None:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
        return int(first_line)
    except (OSError, IndexError, ValueError):
        return None


def _connect(database: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _ensure_schema(database: str | Path) -> None:
    with _connect(database) as connection:
        connection.execute(SCHEDULE_RUNS_TABLE)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduler_runs_mode_date "
            "ON scheduler_runs(mode, scheduled_at_utc)"
        )


def _latest_trade_date(database: str | Path) -> str | None:
    store = RevisionStore(database)
    dates = [
        str(record.get("trade_date") or "")
        for row in store.current_records("daily_bars")
        if (record := (row.get("record") if isinstance(row.get("record"), Mapping) else row))
        and str(record.get("trade_date") or "")
    ]
    return max(dates) if dates else None


def _has_daily_bars_for_date(database: str | Path, trade_date: str) -> bool:
    _ensure_schema(database)
    prefix = f'{{"trade_date":"{trade_date}"%'
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT 1 FROM data_revisions "
            "WHERE dataset='daily_bars' AND natural_key_json LIKE ? LIMIT 1",
            (prefix,),
        ).fetchone()
    return row is not None


def _latest_daily_decision(database: str | Path) -> str | None:
    with _connect(database) as connection:
        connection.execute(SCHEDULE_RUNS_TABLE)
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_runs'"
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            "SELECT MAX(decision_date) FROM daily_runs "
            "WHERE status IN ('completed','blocked_data_gate')"
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _latest_postclose_decision(database: str | Path) -> str | None:
    _ensure_schema(database)
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT MAX(decision_date) FROM scheduler_runs "
            "WHERE mode='postclose' AND status IN ('completed','no_new_data')"
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _record_start(
    database: str | Path,
    *,
    mode: str,
    log_path: Path,
    command: Mapping[str, Any],
) -> str:
    _ensure_schema(database)
    run_id = uuid.uuid4().hex
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO scheduler_runs (
                run_id, mode, scheduled_at_utc, started_at_utc, status,
                command_json, log_path
            ) VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                run_id,
                mode,
                _utc_now(),
                _utc_now(),
                _canonical_json(command),
                str(log_path),
            ),
        )
    return run_id


def _record_finish(
    database: str | Path,
    run_id: str,
    *,
    status: str,
    decision_date: str | None = None,
    error_text: str | None = None,
) -> None:
    _ensure_schema(database)
    with _connect(database) as connection:
        connection.execute(
            """
            UPDATE scheduler_runs
            SET completed_at_utc=?, status=?, decision_date=?, error_text=?
            WHERE run_id=?
            """,
            (_utc_now(), status, decision_date, error_text, run_id),
        )


def _reconcile_stale_runs(
    database: str | Path,
    project_root: Path,
    *,
    stale_after_seconds: float = 21_600.0,
) -> list[str]:
    _ensure_schema(database)
    reconciled: list[str] = []
    now = datetime.now(UTC)
    with _connect(database) as connection:
        rows = connection.execute(
            "SELECT run_id, mode, started_at_utc FROM scheduler_runs "
            "WHERE status='running' ORDER BY scheduled_at_utc"
        ).fetchall()
        for row in rows:
            lock_path = project_root / "runtime" / "scheduler" / f"{row['mode']}.lock"
            lock_active = False
            if lock_path.is_file():
                try:
                    started = datetime.fromisoformat(str(row["started_at_utc"]).replace("Z", "+00:00"))
                    age = (now - started).total_seconds()
                    pid = _lock_pid(lock_path)
                    if age <= stale_after_seconds and pid is not None:
                        lock_active = _process_exists(pid)
                    elif age <= 60:
                        lock_active = True
                except ValueError:
                    lock_active = False
            if lock_active:
                continue
            connection.execute(
                """
                UPDATE scheduler_runs
                SET status='failed', completed_at_utc=?, error_text=?
                WHERE run_id=? AND status='running'
                """,
                (
                    _utc_now(),
                    "stale running run reconciled: process exited before completion",
                    row["run_id"],
                ),
            )
            reconciled.append(str(row["run_id"]))
    return reconciled


class _SchedulerLock:
    def __init__(self, path: Path, *, stale_after_seconds: float = 21_600.0) -> None:
        self.path = path
        self.stale_after_seconds = float(stale_after_seconds)
        self.acquired = False

    def __enter__(self) -> "_SchedulerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if age > self.stale_after_seconds:
                self.path.unlink(missing_ok=True)
            else:
                pid = _lock_pid(self.path)
                if (pid is not None and not _process_exists(pid)) or (
                    pid is None and age > 60
                ):
                    self.path.unlink(missing_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError as exc:
            raise RuntimeError(f"scheduler lock is already held: {self.path}") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n{_utc_now()}\n")
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
        self.acquired = False


def _run_command(
    arguments: Sequence[str],
    *,
    log_path: Path,
    allowed_exit_codes: Sequence[int] = (0,),
) -> int:
    environment = os.environ.copy()
    source_root = str(PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not existing else source_root + os.pathsep + existing
    )
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"[{_utc_now()}] command: {json.dumps(list(arguments))}\n")
        handle.flush()
        result = subprocess.run(
            [sys.executable, "-m", "kinace_v3", *arguments],
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if result.returncode not in set(int(code) for code in allowed_exit_codes):
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: {' '.join(arguments)}"
        )
    return int(result.returncode)


def _backup_database(database: str | Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"kinace_v3_2-{datetime.now(CHINA_TZ):%Y%m%d}.sqlite3"
    temporary = target.with_suffix(".sqlite3.tmp")
    source = sqlite3.connect(str(database))
    destination = sqlite3.connect(str(temporary))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    verification = sqlite3.connect(str(temporary))
    try:
        result = verification.execute("PRAGMA integrity_check").fetchone()
    finally:
        verification.close()
    if not result or str(result[0]).lower() != "ok":
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"backup integrity check failed: {result}")
    temporary.replace(target)
    target.with_suffix(target.suffix + ".sha256").write_text(
        _sha256_file(target) + "\n",
        encoding="ascii",
    )
    backups = sorted(backup_dir.glob("kinace_v3_2-*.sqlite3"), reverse=True)
    for stale in backups[30:]:
        stale.unlink(missing_ok=True)
    return target


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def restore_database(backup_path: str | Path, database: str | Path) -> Path:
    source = Path(backup_path).resolve(strict=False)
    target = Path(database).resolve(strict=False)
    if not source.is_file():
        raise RuntimeError(f"backup does not exist: {source}")
    verification = sqlite3.connect(str(source))
    try:
        result = verification.execute("PRAGMA integrity_check").fetchone()
    finally:
        verification.close()
    if not result or str(result[0]).lower() != "ok":
        raise RuntimeError(f"backup integrity check failed: {result}")
    sidecar = source.with_suffix(source.suffix + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="ascii").strip().lower()
        if _sha256_file(source) != expected:
            raise RuntimeError("backup SHA256 sidecar does not match")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".restore.tmp")
    temporary.write_bytes(source.read_bytes())
    temporary.replace(target)
    return target


def _write_alert(report_dir: Path, *, mode: str, error: str) -> Path:
    alert_dir = report_dir / "alerts"
    alert_dir.mkdir(parents=True, exist_ok=True)
    target = alert_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{mode}.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "created_at_utc": _utc_now(),
                "mode": mode,
                "error": error,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _alert_webhook_url() -> str:
    return str(os.environ.get("KINACE_ALERT_WEBHOOK_URL") or "").strip()


def _alert_webhook_status() -> dict[str, Any]:
    url = _alert_webhook_url()
    parsed = urlparse(url)
    if not url:
        return {"configured": False, "host": None}
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {"configured": False, "host": None, "invalid": True}
    return {"configured": True, "host": parsed.hostname}


def _send_alert_webhook(*, mode: str, error: str, alert_path: Path) -> dict[str, Any]:
    url = _alert_webhook_url()
    if not url:
        return {"status": "not_configured", "url": None}
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": _utc_now(),
            "mode": mode,
            "error": error,
            "alert_path": str(alert_path),
        },
        ensure_ascii=True,
        sort_keys=True,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status_code = int(getattr(response, "status", 200))
        return {"status": "sent", "url": url, "http_status": status_code}
    except (OSError, urllib.error.URLError) as exc:
        return {"status": "failed", "url": url, "error": str(exc)}


def _invoke_runner(
    runner: Callable[..., int],
    arguments: Sequence[str],
    *,
    database: str | Path,
    log_path: Path,
    allowed_exit_codes: Sequence[int] = (0,),
) -> int:
    resolved_arguments = list(arguments)
    if "--database" not in resolved_arguments:
        resolved_arguments.extend(["--database", str(database)])
    return runner(
        resolved_arguments,
        log_path=log_path,
        allowed_exit_codes=tuple(allowed_exit_codes),
    )


def _execute_preopen(
    *,
    database: str | Path,
    project_root: Path,
    log_path: Path,
    runner: Callable[..., int],
) -> tuple[str, str | None]:
    decision_date = _latest_trade_date(database)
    if decision_date is None:
        raise RuntimeError("cannot run preopen strategy without daily bars")
    settled_date = _latest_postclose_decision(database)
    if settled_date != decision_date:
        raise RuntimeError(
            "preopen strategy is blocked because the latest trade date has not "
            f"completed post-close settlement: trade={decision_date}, settled={settled_date}"
        )
    _invoke_runner(
        runner,
        [
            "daily-run",
            "--run-purpose",
            "preopen",
            "--decision-date",
            decision_date,
            "--snapshot-dir",
            str(CANONICAL_SNAPSHOT_DIR),
            "--candidate-config",
            str(project_root / "reports" / "baseline_v32_candidate.json"),
            "--skip-market-sync",
            "--skip-portfolio",
        ],
        database=database,
        log_path=log_path,
    )
    return "completed", decision_date


def _execute_postclose(
    *,
    database: str | Path,
    project_root: Path,
    log_path: Path,
    runner: Callable[..., int],
    force: bool = False,
    latest_only: bool = False,
) -> tuple[str, str | None]:
    china_today = datetime.now(CHINA_TZ).date()
    if latest_only:
        start = china_today
    else:
        latest = _latest_trade_date(database)
        start = (
            china_today - timedelta(days=14)
            if latest is None
            else datetime.fromisoformat(latest).date() - timedelta(days=14)
        )
    sync_arguments = [
        "market-sync",
        "--provider",
        "tushare",
        "--all-metadata",
        "--start-date",
        start.isoformat(),
        "--end-date",
        china_today.isoformat(),
        "--timeout",
        "60",
        "--max-retries",
        "4",
        "--retry-backoff",
        "1.0",
        "--parity-output",
        str(PARITY_PATH),
        "--json",
    ]
    if force:
        sync_arguments.append("--rebase-price-basis")
    if latest_only:
        sync_arguments.append("--latest-only")
    _invoke_runner(
        runner,
        sync_arguments,
        database=database,
        log_path=log_path,
    )
    if latest_only:
        new_latest = (
            china_today.isoformat()
            if _has_daily_bars_for_date(database, china_today.isoformat())
            else _latest_postclose_decision(database)
        )
    else:
        new_latest = _latest_trade_date(database)
    if new_latest is None:
        raise RuntimeError("market sync produced no daily bars")
    if not force and _latest_postclose_decision(database) == new_latest:
        return "no_new_data", new_latest
    _invoke_runner(
        runner,
        [
            "daily-run",
            "--run-purpose",
            "postclose",
            "--decision-date",
            new_latest,
            "--snapshot-dir",
            str(CANONICAL_SNAPSHOT_DIR),
            "--candidate-config",
            str(project_root / "reports" / "baseline_v32_candidate.json"),
            "--portfolio-config",
            str(project_root / "reports" / "baseline_v32_backtest_config.json"),
            "--skip-market-sync",
            "--json",
        ],
        database=database,
        log_path=log_path,
    )
    _execute_holdout_extension(
        database=database,
        project_root=project_root,
        log_path=log_path,
        runner=runner,
    )
    return "completed", new_latest


def _execute_weekly(
    *,
    database: str | Path,
    project_root: Path,
    log_path: Path,
    runner: Callable[..., int],
) -> tuple[str, str | None]:
    if _pending_holdout_freeze(project_root, database) is not None:
        return _execute_holdout_extension(
            database=database,
            project_root=project_root,
            log_path=log_path,
            runner=runner,
        )
    preflight_output = project_root / "reports" / "research_audit_preflight.json"
    preflight_markdown = project_root / "reports" / "research_audit_preflight.md"
    _invoke_runner(
        runner,
        [
            "research-audit",
            "--preflight",
            "--snapshot-manifest",
            str(CANONICAL_SNAPSHOT_DIR / "manifest.json"),
            "--parity",
            str(PARITY_PATH),
            "--output",
            str(preflight_output),
            "--markdown",
            str(preflight_markdown),
        ],
        database=database,
        log_path=log_path,
    )
    preflight = json.loads(preflight_output.read_text(encoding="utf-8"))
    if preflight.get("research_eligible") is not True:
        raise RuntimeError("weekly research preflight audit is not research eligible")

    loop_output = project_root / "reports" / "research_loop_scheduled.json"
    loop_output.unlink(missing_ok=True)
    _invoke_runner(
        runner,
        [
            "research-loop",
            "--input",
            str(project_root / "config" / "optimization.v32.json"),
            "--snapshot-dir",
            str(CANONICAL_SNAPSHOT_DIR),
            "--audit-report",
            str(preflight_output),
            "--output",
            str(loop_output),
            "--output-dir",
            str(project_root / "outputs" / "research_loop_scheduled"),
            "--max-rounds",
            "4",
        ],
        database=database,
        log_path=log_path,
        allowed_exit_codes=(0, 2),
    )
    result = json.loads(loop_output.read_text(encoding="utf-8"))
    status = str(result.get("status") or "")
    if status not in {"challenger_ready", "no_eligible_challenger"}:
        raise RuntimeError(f"weekly research loop returned unexpected status: {status}")
    holdout = result.get("holdout") if isinstance(result, Mapping) else None
    candidate_report = (
        holdout.get("portfolio_output_path") if isinstance(holdout, Mapping) else None
    )
    if not candidate_report:
        raise RuntimeError("new research-loop report has no holdout portfolio artifact")

    stability_output = project_root / "reports" / "research_stability_scheduled.json"
    stability_markdown = project_root / "reports" / "research_stability_scheduled.md"
    _invoke_runner(
        runner,
        [
            "research-stability",
            "--research-report",
            str(loop_output),
            "--snapshot-dir",
            str(CANONICAL_SNAPSHOT_DIR),
            "--output",
            str(stability_output),
            "--markdown",
            str(stability_markdown),
            "--output-dir",
            str(project_root / "outputs" / "research_stability_scheduled"),
        ],
        database=database,
        log_path=log_path,
        allowed_exit_codes=(0, 2),
    )
    stability = json.loads(stability_output.read_text(encoding="utf-8"))
    if stability.get("status") not in {"passed", "review_required"}:
        raise RuntimeError("weekly research stability validation returned an invalid status")

    if result.get("holdout_status") == "holdout_extension_pending":
        return "holdout_extension_pending", str(result.get("loop_id") or "")

    candidate_audit_output = project_root / "reports" / "research_audit_candidate.json"
    candidate_audit_markdown = project_root / "reports" / "research_audit_candidate.md"
    _invoke_runner(
        runner,
        [
            "research-audit",
            "--candidate-report",
            str(candidate_report),
            "--loop-report",
            str(loop_output),
            "--bind-loop",
            "--snapshot-manifest",
            str(CANONICAL_SNAPSHOT_DIR / "manifest.json"),
            "--parity",
            str(PARITY_PATH),
            "--output",
            str(candidate_audit_output),
            "--markdown",
            str(candidate_audit_markdown),
        ],
        database=database,
        log_path=log_path,
    )
    candidate_audit = json.loads(candidate_audit_output.read_text(encoding="utf-8"))
    if candidate_audit.get("candidate_audit_passed") is not True:
        raise RuntimeError("weekly candidate audit did not pass with a bound challenger")
    return "completed", str(result.get("loop_id") or "")


def _extension_registry_path(project_root: Path) -> Path:
    return project_root / "reports" / "holdout_extension_registry.json"


def _pending_holdout_freeze(
    project_root: Path,
    database: str | Path,
) -> Path | None:
    records = pending_holdout_extension_records(
        registry_path=_extension_registry_path(project_root),
    )
    if records:
        return Path(str(records[0]["freeze_path"]))
    loop_output = project_root / "reports" / "research_loop_scheduled.json"
    if not loop_output.is_file():
        return None
    try:
        result = json.loads(loop_output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, Mapping):
        return None
    if result.get("holdout_status") != "holdout_extension_pending":
        return None
    holdout = result.get("holdout")
    if not isinstance(holdout, Mapping):
        return None
    freeze_path = holdout.get("freeze_path")
    if not isinstance(freeze_path, str) or not Path(freeze_path).is_file():
        return None
    return Path(freeze_path)


def _execute_holdout_extension(
    *,
    database: str | Path,
    project_root: Path,
    log_path: Path,
    runner: Callable[..., int],
) -> tuple[str, str | None]:
    registry_records = pending_holdout_extension_records(
        registry_path=_extension_registry_path(project_root),
    )
    if not registry_records:
        freeze_path = _pending_holdout_freeze(project_root, database)
        if freeze_path is None:
            return "not_pending", None
        registry_records = [
            {
                "database": str(database),
                "freeze_id": "",
                "freeze_path": str(freeze_path),
            }
        ]
    statuses: list[str] = []
    last_extension_id: str | None = None
    for registry_record in registry_records:
        freeze_path = Path(str(registry_record["freeze_path"]))
        run_database = str(registry_record.get("database") or database)
        freeze_key = str(registry_record.get("freeze_id") or "manual")[:12]
        output_path = (
            project_root
            / "reports"
            / f"holdout_extension_scheduled-{freeze_key}.json"
        )
        markdown_path = output_path.with_suffix(".md")
        _invoke_runner(
            runner,
            [
                "research-holdout-extension",
                "--freeze",
                str(freeze_path),
                "--database",
                run_database,
                "--snapshot-dir",
                str(CANONICAL_SNAPSHOT_DIR),
                "--output",
                str(output_path),
                "--markdown",
                str(markdown_path),
                "--output-dir",
                str(project_root / "outputs" / "holdout_extension_scheduled"),
            ],
            database=database,
            log_path=log_path,
            allowed_exit_codes=(0, 2),
        )
        result = json.loads(output_path.read_text(encoding="utf-8"))
        status = str(result.get("status") or "")
        if status not in {"passed", "rejected", "awaiting_data"}:
            raise RuntimeError(f"holdout extension returned unexpected status: {status}")
        extension_id = str(result.get("extension_id") or "")
        statuses.append(status)
        last_extension_id = extension_id or last_extension_id
        if registry_record.get("freeze_id"):
            update_holdout_extension_registry(
                database=run_database,
                freeze_id=str(registry_record["freeze_id"]),
                status=status,
                extension_id=extension_id,
                result_path=output_path,
                registry_path=_extension_registry_path(project_root),
            )
    if statuses and all(status == "awaiting_data" for status in statuses):
        return "awaiting_data", last_extension_id
    return "completed", last_extension_id


def delivery_status(
    *,
    database: str | Path,
    report_dir: Path,
) -> dict[str, Any]:
    _ensure_schema(database)
    with _connect(database) as connection:
        scheduler_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT run_id, status, decision_date, started_at_utc,
                       completed_at_utc, error_text
                FROM scheduler_runs
                WHERE mode='postclose'
                ORDER BY scheduled_at_utc DESC
                """
            ).fetchall()
        ]
        daily_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_runs'"
        ).fetchone()
        daily_rows = (
            [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT run_id, decision_date, status, snapshot_id, replay_id,
                           backtest_run_id, manifest_path, report_path
                    FROM daily_runs
                    ORDER BY decision_date DESC, completed_at_utc DESC
                    """
                ).fetchall()
            ]
            if daily_table is not None
            else []
        )
    successful_dates = sorted(
        {
            str(row.get("decision_date"))
            for row in scheduler_rows
            if row.get("status") in SUCCESS_STATUSES and row.get("decision_date")
        },
        reverse=True,
    )
    unattended_dates = successful_dates[:20]
    successful_date_set = set(successful_dates)
    relevant_daily_rows = [
        row
        for row in daily_rows
        if row.get("decision_date") in successful_date_set
        and bool(row.get("backtest_run_id"))
    ]
    missing_artifacts: list[str] = []
    incomplete_lineage: list[str] = []
    settled_daily_dates: set[str] = set()
    daily_date_counts = Counter(
        str(row.get("decision_date")) for row in relevant_daily_rows
    )
    duplicate_daily_runs = {
        date: count for date, count in daily_date_counts.items() if count > 1
    }
    for row in relevant_daily_rows:
        run_id = str(row.get("run_id") or "")
        complete = str(row.get("status") or "") == "completed"
        for field in ("snapshot_id", "replay_id", "backtest_run_id"):
            if not row.get(field):
                incomplete_lineage.append(f"{run_id}:{field}")
                complete = False
        for field in ("manifest_path", "report_path"):
            path = row.get(field)
            if not path or not Path(str(path)).is_file():
                missing_artifacts.append(f"{run_id}:{field}")
                complete = False
        if complete and row.get("decision_date"):
            settled_daily_dates.add(str(row["decision_date"]))
    project_root = report_dir.parent.parent
    audit_path = project_root / "reports" / "research_audit_canonical.json"
    audit = (
        json.loads(audit_path.read_text(encoding="utf-8"))
        if audit_path.is_file()
        else {}
    )
    failed_scheduler_runs = [
        row for row in scheduler_rows if row.get("status") == "failed"
    ]
    successful_runs = [
        row for row in scheduler_rows if row.get("status") in SUCCESS_STATUSES
    ]
    unresolved_failed_runs = []
    resolved_failed_runs = []
    for failed in failed_scheduler_runs:
        failed_at = str(failed.get("started_at_utc") or "")
        failed_day = failed_at[:10]
        resolved = any(
            str(success.get("started_at_utc") or "") > failed_at
            or (
                failed_day
                and str(success.get("started_at_utc") or "")[:10] == failed_day
            )
            for success in successful_runs
        )
        (resolved_failed_runs if resolved else unresolved_failed_runs).append(failed)
    ready = bool(
        len(unattended_dates) >= 20
        and set(unattended_dates).issubset(settled_daily_dates)
        and not missing_artifacts
        and not incomplete_lineage
        and not duplicate_daily_runs
        and audit.get("research_eligible") is True
        and audit.get("live_sync_eligible") is True
        and not unresolved_failed_runs
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": _utc_now(),
        "ready": ready,
        "unattended_trading_days": len(unattended_dates),
        "unattended_dates": unattended_dates,
        "latest_daily_run": (
            relevant_daily_rows[0] if relevant_daily_rows else None
        ),
        "latest_scheduler_run": next(
            (
                row
                for row in scheduler_rows
                if row.get("status") in SUCCESS_STATUSES
            ),
            next(
                (
                    row
                    for row in scheduler_rows
                    if row.get("status") == "failed"
                ),
                scheduler_rows[0] if scheduler_rows else None,
            ),
        ),
        "latest_scheduler_failure": next(
            (row for row in scheduler_rows if row.get("status") == "failed"),
            None,
        ),
        "active_scheduler_runs": [
            row for row in scheduler_rows if row.get("status") == "running"
        ],
        "missing_artifacts": missing_artifacts,
        "incomplete_lineage": incomplete_lineage,
        "duplicate_daily_runs": duplicate_daily_runs,
        "failed_scheduler_runs": failed_scheduler_runs[:10],
        "resolved_failed_runs": resolved_failed_runs[:10],
        "unresolved_failed_runs": unresolved_failed_runs[:10],
        "audit": {
            "status": audit.get("status"),
            "research_eligible": audit.get("research_eligible"),
            "live_sync_eligible": audit.get("live_sync_eligible"),
        },
        "production_ready": False,
        "production_changed": False,
        "alert_webhook": _alert_webhook_status(),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / "latest-status.json"
    target.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ready_path = report_dir / "delivery-ready.json"
    if ready:
        ready_path.write_text(
            json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        ready_path.unlink(missing_ok=True)
    return result


def execute_mode(
    mode: str,
    *,
    database: str | Path = DEFAULT_DATABASE_PATH,
    report_dir: Path = PROJECT_ROOT / "reports" / "scheduler",
    runner: Callable[..., int] = _run_command,
    force: bool = False,
    latest_only: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    project_root = report_dir.parent.parent
    _reconcile_stale_runs(database, project_root)
    if mode == "status":
        return delivery_status(database=database, report_dir=report_dir)
    if mode not in {"preopen", "postclose", "weekly", "extension"}:
        raise RuntimeError(f"unsupported scheduler mode: {mode}")
    log_dir = project_root / "logs" / "scheduler"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{mode}-{datetime.now(CHINA_TZ):%Y%m%d}.log"
    lock_path = project_root / "runtime" / "scheduler" / f"{mode}.lock"
    with _SchedulerLock(lock_path):
        run_id = _record_start(
            database,
            mode=mode,
            log_path=log_path,
            command={"mode": mode, "database": str(database)},
        )
        try:
            if mode == "preopen":
                status, decision_date = _execute_preopen(
                    database=database,
                    project_root=project_root,
                    log_path=log_path,
                    runner=runner,
                )
            elif mode == "postclose":
                status, decision_date = _execute_postclose(
                    database=database,
                    project_root=project_root,
                    log_path=log_path,
                    runner=runner,
                    force=force,
                    latest_only=latest_only,
                )
                if backup:
                    _backup_database(database, KINACE_DATA_ROOT / "backups")
            elif mode == "extension":
                status, decision_date = _execute_holdout_extension(
                    database=database,
                    project_root=project_root,
                    log_path=log_path,
                    runner=runner,
                )
            else:
                status, decision_date = _execute_weekly(
                    database=database,
                    project_root=project_root,
                    log_path=log_path,
                    runner=runner,
                )
            _record_finish(
                database,
                run_id,
                status=status,
                decision_date=decision_date,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            alert_path = _write_alert(report_dir, mode=mode, error=message)
            notification = _send_alert_webhook(
                mode=mode,
                error=message,
                alert_path=alert_path,
            )
            if notification["status"] == "failed":
                message = f"{message}; notification failed: {notification.get('error')}"
            elif notification["status"] == "not_configured":
                message = f"{message}; notification not configured"
            _record_finish(database, run_id, status="failed", error_text=message)
            raise RuntimeError(
                f"{message}; alert: {alert_path}; notification: {notification['status']}"
            ) from exc
    result = delivery_status(database=database, report_dir=report_dir)
    result.update(
        {
            "mode": mode,
            "scheduler_run_id": run_id,
            "status": status,
            "decision_date": decision_date,
            "log_path": str(log_path),
        }
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("preopen", "postclose", "weekly", "extension", "status"),
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument(
        "--report-dir",
        default=str(PROJECT_ROOT / "reports" / "scheduler"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute_mode(
            args.mode,
            database=args.database,
            report_dir=Path(args.report_dir),
        )
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
