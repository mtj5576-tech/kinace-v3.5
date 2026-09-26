from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from kinace_v3.launcher import _run_daily_update, _today_postclose_run
from kinace_v3.schedule_runner import CHINA_TZ


def _today() -> str:
    return datetime.now(CHINA_TZ).date().isoformat()


def _seed_scheduler_run(database, *, status: str, decision_date: str | None) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE scheduler_runs (
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
        )
        connection.execute(
            "INSERT INTO scheduler_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "existing",
                "postclose",
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                status,
                decision_date,
                "{}",
                "test.log",
                None,
            ),
        )


def test_today_postclose_run_uses_china_trading_date(tmp_path):
    database = tmp_path / "v32.sqlite3"
    _seed_scheduler_run(database, status="completed", decision_date=_today())

    row = _today_postclose_run(database)

    assert row is not None
    assert row["status"] == "completed"
    assert row["decision_date"] == _today()


def test_daily_update_skips_a_successful_same_day_run(tmp_path):
    database = tmp_path / "v32.sqlite3"
    _seed_scheduler_run(database, status="completed", decision_date=_today())
    state = {"status": "checking", "message": "checking"}
    calls = 0

    def updater(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("updater must not run after a successful same-day update")

    _run_daily_update(
        project_root=tmp_path,
        database=database,
        state=state,
        lock=threading.Lock(),
        updater=updater,
    )

    assert calls == 0
    assert state["status"] == "completed"
    assert state["message"] == "今日数据更新完成"


def test_daily_update_runs_when_today_has_no_success(tmp_path):
    database = tmp_path / "v32.sqlite3"
    state = {"status": "checking", "message": "checking"}
    calls = 0

    def updater(mode, **kwargs):
        nonlocal calls
        calls += 1
        assert mode == "postclose"
        return {
            "status": "completed",
            "decision_date": _today(),
            "scheduler_run_id": "new-run",
            "log_path": "new.log",
        }

    _run_daily_update(
        project_root=tmp_path,
        database=database,
        state=state,
        lock=threading.Lock(),
        updater=updater,
    )

    assert calls == 1
    assert state["status"] == "completed"
    assert state["decision_date"] == _today()
    assert state["scheduler_run_id"] == "new-run"


def test_daily_update_retries_later_when_earlier_run_had_no_new_data(tmp_path):
    database = tmp_path / "v32.sqlite3"
    previous = (datetime.now(CHINA_TZ).date() - timedelta(days=1)).isoformat()
    _seed_scheduler_run(database, status="no_new_data", decision_date=previous)
    state = {"status": "checking", "message": "checking"}
    calls = 0

    def updater(mode, **kwargs):
        nonlocal calls
        calls += 1
        return {"status": "completed", "decision_date": _today()}

    _run_daily_update(
        project_root=tmp_path,
        database=database,
        state=state,
        lock=threading.Lock(),
        updater=updater,
    )

    assert calls == 1
    assert state["decision_date"] == _today()


def test_latest_only_update_skips_same_day_no_new_data_check(tmp_path):
    database = tmp_path / "v32.sqlite3"
    previous = (datetime.now(CHINA_TZ).date() - timedelta(days=1)).isoformat()
    _seed_scheduler_run(database, status="no_new_data", decision_date=previous)
    state = {"status": "checking", "message": "checking"}

    def updater(*args, **kwargs):
        raise AssertionError("same-day no_new_data check must not run again")

    _run_daily_update(
        project_root=tmp_path,
        database=database,
        state=state,
        lock=threading.Lock(),
        latest_only=True,
        backup=False,
        updater=updater,
    )

    assert state["status"] == "completed"


def test_daily_update_exposes_updating_state_while_work_is_running(tmp_path):
    database = tmp_path / "v32.sqlite3"
    state = {"status": "checking", "message": "checking"}
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def updater(mode, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return {"status": "completed", "decision_date": _today()}

    worker = threading.Thread(
        target=_run_daily_update,
        kwargs={
            "project_root": tmp_path,
            "database": database,
            "state": state,
            "lock": lock,
            "updater": updater,
        },
    )
    worker.start()
    try:
        assert started.wait(timeout=5)
        assert state["status"] == "updating"
        assert state["message"] == "正在更新市场数据并生成今日策略"
    finally:
        release.set()
        worker.join(timeout=5)

    assert state["status"] == "completed"
