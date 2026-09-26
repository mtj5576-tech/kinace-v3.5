from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from kinace_v3.config import ForbiddenDatabasePath
from kinace_v3.data_collection import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    DailyBasicMetricRecord,
    ForwardLabelRecord,
    NameChangeIntervalRecord,
    RevisionStatus,
    RevisionStore,
    SectorMembershipRecord,
    SecurityStateRecord,
    StockMetadataRecord,
    TradeCalendarRecord,
)


def test_store_migrates_v1_revision_check_and_preserves_rows(tmp_path):
    database = tmp_path / "v1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE ingestion_runs (
                run_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE data_revisions (
                revision_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id TEXT NOT NULL UNIQUE,
                schema_version INTEGER NOT NULL,
                dataset TEXT NOT NULL CHECK (dataset IN (
                    'trade_calendar','stock_metadata','daily_bars','daily_basic_metrics',
                    'adjustment_factors','name_change_intervals','sector_membership'
                )),
                natural_key_json TEXT NOT NULL,
                status TEXT NOT NULL,
                record_json TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                ingestion_run_id TEXT NOT NULL,
                supersedes_revision_id TEXT
            );
            INSERT INTO ingestion_runs VALUES (
                'run-v1', 'fixture', '2024-01-01T00:00:00Z', '2024-01-01T00:00:01Z',
                'completed', '{}'
            );
            INSERT INTO data_revisions (
                revision_id, schema_version, dataset, natural_key_json, status,
                record_json, source, observed_at_utc, recorded_at_utc,
                ingestion_run_id, supersedes_revision_id
            ) VALUES (
                'old-revision', 1, 'daily_bars',
                '{"trade_date":"2024-01-02","ts_code":"000001.SZ"}', 'observed',
                '{"close":10.5,"high":11.0,"low":9.8,"open":10.0,"trade_date":"2024-01-02","ts_code":"000001.SZ"}',
                'fixture', '2024-01-02T00:00:00Z', '2024-01-02T00:00:01Z', 'run-v1', NULL
            );
            """
        )

    store = RevisionStore(database)
    records = store.current_records("daily_bars")

    assert len(records) == 1
    assert records[0]["record"]["ts_code"] == "000001.SZ"
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            )
        }
    assert "current_security_state" in tables
    assert "current_forward_labels" in tables


def test_store_write_does_not_wait_for_an_open_reader(tmp_path):
    database = tmp_path / "wal.sqlite3"
    store = RevisionStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    reader = sqlite3.connect(database)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM data_revisions").fetchone()
    result: dict[str, object] = {}

    def write_revision() -> None:
        try:
            result["revision_id"] = store.append_revision(
                _daily_bar(), source="offline"
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    worker = threading.Thread(target=write_revision)
    try:
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive(), "writer waited for the open reader"
    finally:
        reader.rollback()
        reader.close()
        worker.join(timeout=5)

    assert "error" not in result
    assert isinstance(result.get("revision_id"), str)


def _daily_bar(close: float = 10.0) -> DailyBarRecord:
    return DailyBarRecord(
        ts_code="000001.SZ",
        trade_date="2024-01-02",
        open=9.8,
        high=10.2,
        low=9.7,
        close=close,
        pre_close=9.9,
        change=close - 9.9,
        pct_chg=(close - 9.9) / 9.9 * 100,
        vol=123456.0,
        amount=987654.0,
    )


def test_revision_ids_are_deterministic_and_ingestion_runs_are_recorded(tmp_path):
    first_store = RevisionStore(tmp_path / "first.sqlite3")
    second_store = RevisionStore(tmp_path / "second.sqlite3")

    first_id = first_store.append_revision(
        _daily_bar(), status=RevisionStatus.BACKFILLED_RELIABLE, source="offline"
    )
    second_id = second_store.append_revision(
        _daily_bar(), status=RevisionStatus.BACKFILLED_RELIABLE, source="offline"
    )

    assert first_id == second_id
    assert len(first_id) == 64
    assert first_store.get_revision(first_id)["record"] == _daily_bar().to_dict()
    runs = first_store.list_ingestion_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["metadata"]["revision_id"] == first_id


def test_database_triggers_prevent_revision_update_and_delete(tmp_path):
    database = tmp_path / "immutable.sqlite3"
    store = RevisionStore(database)
    revision_id = store.append_revision(_daily_bar(), status=RevisionStatus.OBSERVED)

    with sqlite3.connect(str(database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE data_revisions SET status='invalid' WHERE revision_id=?",
                (revision_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM data_revisions WHERE revision_id=?", (revision_id,)
            )
        connection.rollback()

    assert store.get_revision(revision_id)["status"] == "observed"
    assert store.count_revisions() == 1


def test_current_view_selects_latest_non_superseded_revision(tmp_path):
    database = tmp_path / "current.sqlite3"
    store = RevisionStore(database)

    first_id = store.append_revision(_daily_bar(close=10.0), source="offline")
    second_id = store.append_revision(
        _daily_bar(close=11.0),
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="offline",
        supersedes_revision_id=first_id,
    )

    current = store.current_revision(
        "daily_bars", {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}
    )
    assert current is not None
    assert current["revision_id"] == second_id
    assert current["record"]["close"] == 11.0
    assert current["effective_status"] == "backfilled_reliable"
    assert current["gate_eligible"] == 1
    assert current["supersedes_revision_id"] == first_id

    history = store.history(
        "daily_bars", {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}
    )
    assert [row["revision_id"] for row in history] == [second_id, first_id]
    assert history[1]["effective_status"] == "superseded"
    assert history[1]["superseded_by_revision_id"] == second_id
    assert store.count_revisions() == 2

    with sqlite3.connect(str(database)) as connection:
        rows = connection.execute("SELECT revision_id FROM current_daily_bars").fetchall()
    assert rows == [(second_id,)]


def test_provisional_revision_is_replaced_without_deletion(tmp_path):
    database = tmp_path / "provisional.sqlite3"
    store = RevisionStore(database)

    provisional_id = store.append_revision(
        _daily_bar(close=10.0),
        status=RevisionStatus.PROVISIONAL,
        source="provisional-baseline",
    )
    provisional = store.current_revision(
        "daily_bars", {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}
    )
    assert provisional["revision_id"] == provisional_id
    assert provisional["gate_eligible"] == 0
    assert provisional["training_eligible"] == 0

    observed_id = store.append_revision(
        _daily_bar(close=10.0),
        status=RevisionStatus.OBSERVED,
        source="provider",
        supersedes_revision_id=provisional_id,
    )
    current = store.current_revision(
        "daily_bars", {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}
    )
    assert current["revision_id"] == observed_id
    assert current["status"] == "observed"
    assert current["gate_eligible"] == 1
    assert current["training_eligible"] == 1
    assert store.count_revisions() == 2
    assert store.get_revision(provisional_id)["effective_status"] == "superseded"


def test_all_canonical_record_types_round_trip_through_current_views(tmp_path):
    store = RevisionStore(tmp_path / "records.sqlite3")
    records = [
        TradeCalendarRecord(
            exchange="SSE", cal_date="2024-01-02", is_open=True, pretrade_date=None
        ),
        StockMetadataRecord(ts_code="000001.SZ", name="PAYH", industry="Bank"),
        _daily_bar(),
        DailyBasicMetricRecord(
            ts_code="000001.SZ", trade_date="2024-01-02", turnover_rate=1.2
        ),
        AdjustmentFactorRecord(
            ts_code="000001.SZ", trade_date="2024-01-02", adj_factor=1.0
        ),
        NameChangeIntervalRecord(
            ts_code="000001.SZ",
            name="PAYH",
            start_date="2024-01-01",
            end_date="2024-12-31",
            ann_date="2023-12-20",
        ),
        SectorMembershipRecord(
            index_code="000300.SH",
            con_code="000001.SZ",
            in_date="2024-01-01",
            out_date=None,
            is_new="Y",
        ),
        SecurityStateRecord(
            ts_code="000001.SZ",
            trade_date="2024-01-02",
            is_suspended=False,
            is_limit_up=False,
            is_limit_down=False,
            prev_close=9.9,
        ),
        ForwardLabelRecord(
            ts_code="000001.SZ",
            decision_date="2024-01-02",
            next_trade_date="2024-01-03",
            next_open=10.0,
            next_close=10.5,
        ),
    ]

    for record in records:
        store.append_revision(record, source="offline")

    for record in records:
        current = store.current_revision(record.dataset, record.natural_key())
        assert current is not None
        assert current["record"] == record.to_dict()
        assert current["gate_eligible"] == 1


def test_forbidden_v2_tree_is_rejected_before_any_write(tmp_path):
    del tmp_path
    forbidden_root = Path(r"D:\kinacesql")
    forbidden = forbidden_root / "kinace_v3_data_collection_guard_test.sqlite3"
    v2_files = sorted(forbidden_root.glob("kinace_v2*.sqlite3*"))
    before = {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in v2_files
    }

    with pytest.raises(ForbiddenDatabasePath):
        RevisionStore(forbidden)

    after = {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(forbidden_root.glob("kinace_v2*.sqlite3*"))
    }
    assert after == before
    assert not forbidden.exists()
