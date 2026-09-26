from __future__ import annotations

from pathlib import Path

import pytest

from kinace_v3.data_collection import (
    DailyBarRecord,
    IngestionError,
    RevisionStatus,
    RevisionStore,
    fill_provisional,
    ingest_dataset,
    ingest_records,
)


def _bar(close: float = 10.5, high: float = 11.0) -> DailyBarRecord:
    return DailyBarRecord(
        ts_code="000001.SZ",
        trade_date="2024-01-02",
        open=10.0,
        high=high,
        low=9.8,
        close=close,
        pre_close=9.9,
        change=close - 9.9,
        pct_chg=(close - 9.9) / 9.9 * 100.0,
        vol=1000.0,
        amount=10000.0,
    )


class FakeProvider:
    source_name = "fake-provider"

    def __init__(self, records):
        self.records = list(records)

    def fetch_daily_bars(self):
        return list(self.records)


def test_provider_ingestion_is_idempotent_and_corrections_supersede(tmp_path):
    store = RevisionStore(tmp_path / "ingest.sqlite3")
    provider = FakeProvider([_bar()])

    first = ingest_dataset(store, "daily_bars", provider)
    second = ingest_dataset(store, "daily_bars", provider)

    assert first.inserted_count == 1
    assert second.inserted_count == 0
    assert second.skipped_count == 1
    assert store.count_revisions() == 1

    corrected = ingest_dataset(
        store,
        "daily_bars",
        FakeProvider([_bar(close=11.0, high=11.5)]),
    )
    assert corrected.inserted_count == 1
    assert corrected.superseded_count == 1
    assert store.count_revisions() == 2

    history = store.history(
        "daily_bars", {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}
    )
    assert len(history) == 2
    assert history[1]["revision_id"] == first.revision_ids[0]
    assert history[1]["effective_status"] == "superseded"
    assert history[0]["record"]["close"] == 11.0


def test_provisional_fill_is_non_eligible_then_real_revision_replaces_it(tmp_path):
    store = RevisionStore(tmp_path / "provisional.sqlite3")
    key = {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}

    filled = fill_provisional(store, "daily_bars", [key], baseline=10.0)
    assert filled.status == "provisional"
    assert filled.gate_eligible is False
    assert filled.training_eligible is False
    assert filled.release_eligible is False

    provisional = store.current_revision("daily_bars", key)
    assert provisional["effective_status"] == "provisional"
    assert provisional["gate_eligible"] == 0
    assert provisional["training_eligible"] == 0
    assert provisional["record"]["close"] == 10.0

    observed = ingest_records(
        store,
        "daily_bars",
        [_bar(close=10.0)],
        status=RevisionStatus.OBSERVED,
        source="provider",
    )
    current = store.current_revision("daily_bars", key)
    assert observed.superseded_count == 1
    assert current["effective_status"] == "observed"
    assert current["gate_eligible"] == 1
    assert current["training_eligible"] == 1
    assert store.count_revisions() == 2

    old = store.get_revision(filled.revision_ids[0])
    assert old["effective_status"] == "superseded"

    ignored = fill_provisional(store, "daily_bars", [key], baseline=10.0)
    assert ignored.inserted_count == 0
    assert ignored.skipped_count == 1
    assert store.current_revision("daily_bars", key)["revision_id"] == current["revision_id"]


def test_ingestion_rejects_duplicate_keys_without_writing(tmp_path):
    store = RevisionStore(tmp_path / "duplicate.sqlite3")
    with pytest.raises(IngestionError, match="duplicate natural key"):
        ingest_records(store, "daily_bars", [_bar(), _bar()], source="provider")
    assert store.count_revisions() == 0


def test_bulk_ingestion_is_append_only_and_idempotent(tmp_path):
    store = RevisionStore(tmp_path / "bulk.sqlite3")
    records = [
        _bar(close=10.5, high=11.0),
        DailyBarRecord(
            ts_code="000001.SZ",
            trade_date="2024-01-03",
            open=10.6,
            high=11.1,
            low=10.4,
            close=10.8,
            vol=1200.0,
            amount=12000.0,
        ),
    ]

    first = ingest_records(store, "daily_bars", records, source="bulk", bulk=True)
    second = ingest_records(store, "daily_bars", records, source="bulk", bulk=True)

    assert first.inserted_count == 2
    assert second.inserted_count == 0
    assert second.skipped_count == 2
    assert store.count_revisions() == 2
