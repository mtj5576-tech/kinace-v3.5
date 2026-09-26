from __future__ import annotations

from kinace_v3.data_collection.ingest import ingest_records
from kinace_v3.data_collection.models import DailyBarRecord, RevisionStatus
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.data_rebuild import apply_canonical_rebuild, plan_canonical_rebuild


def _bar(trade_date: str, close: float) -> DailyBarRecord:
    return DailyBarRecord(
        ts_code="000001",
        trade_date=trade_date,
        open=close,
        high=close + 0.1,
        low=close - 0.1,
        close=close,
        vol=1_000_000,
    )


def test_canonical_price_rebuild_invalidates_noncanonical_segment(tmp_path):
    database = tmp_path / "v32.sqlite3"
    store = RevisionStore(database)
    ingest_records(
        store,
        "daily_bars",
        [_bar("2026-01-01", 10.0)],
        status=RevisionStatus.OBSERVED,
        source="tencent_market",
    )
    ingest_records(
        store,
        "daily_bars",
        [_bar("2026-01-02", 10.1)],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="kinace_v2_1_history",
    )

    plan = plan_canonical_rebuild(database)
    assert plan["invalidate_counts"]["daily_bars"] == 1

    result = apply_canonical_rebuild(database, apply=True)
    assert result["apply"] is True
    assert result["applied"]["daily_bars"]["inserted_count"] == 1
    second_plan = plan_canonical_rebuild(database)
    assert second_plan["invalidate_counts"]["daily_bars"] == 0


def test_snapshot_rebuild_requires_and_records_pit_policy(tmp_path, monkeypatch):
    database = tmp_path / "v32.sqlite3"
    store = RevisionStore(database)
    ingest_records(
        store,
        "daily_bars",
        [_bar("2026-01-01", 10.0)],
        status=RevisionStatus.OBSERVED,
        source="tencent_market",
    )
    capture = {}

    def fake_build_history_snapshots(*args, **kwargs):
        capture.update(kwargs)
        return {
            "snapshot_count": 1,
            "pit_policy": kwargs["pit_policy"],
            "pit_policy_hash": "b" * 64,
            "industry_source": "pit_membership",
            "industry_coverage": 1.0,
            "pit_ready": True,
        }

    monkeypatch.setattr(
        "kinace_v3.data_rebuild.build_history_snapshots",
        fake_build_history_snapshots,
    )

    result = apply_canonical_rebuild(
        database,
        apply=True,
        rebuild_snapshots=True,
        snapshot_dir=tmp_path / "snapshots",
    )

    assert result["snapshots"]["pit_ready"] is True
    assert capture["pit_policy"]["industry_mode"] == "pit_membership"
    assert capture["snapshot_mode"] == "historical"
