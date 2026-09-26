from __future__ import annotations

import pytest

from kinace_v3.data_collection.ingest import ingest_records
from kinace_v3.data_collection.models import (
    DailyBarRecord,
    RevisionStatus,
    SectorMembershipRecord,
    SecurityStateRecord,
    StockMetadataRecord,
)
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.history_features import load_compact_history_pit_context
from kinace_v3.pit_universe import (
    PitUniverseError,
    build_pit_context,
    resolve_pit_policy,
    select_pit_universe,
    select_pit_universe_from_context,
)


def _seed_universe(database) -> None:
    store = RevisionStore(database)
    ingest_records(
        store,
        "stock_metadata",
        [
            StockMetadataRecord(
                ts_code="000001.SZ",
                name="A",
                list_date="2020-01-01",
            ),
            StockMetadataRecord(
                ts_code="000002.SZ",
                name="B",
                list_date="2020-01-01",
            ),
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    bars = []
    states = []
    for code in ("000001.SZ", "000002.SZ"):
        for trade_date in ("2024-04-15", "2024-04-30"):
            bars.append(
                DailyBarRecord(
                    ts_code=code,
                    trade_date=trade_date,
                    open=10.0,
                    high=10.5,
                    low=9.8,
                    close=10.2,
                    vol=1_000_000,
                    amount=20_000_000,
                )
            )
            states.append(
                SecurityStateRecord(
                    ts_code=code,
                    trade_date=trade_date,
                    is_suspended=False,
                    is_limit_up=False,
                    is_limit_down=False,
                    prev_close=10.0,
                )
            )
    ingest_records(
        store,
        "daily_bars",
        bars,
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(
        store,
        "security_state",
        states,
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(
        store,
        "sector_membership",
        [
            SectorMembershipRecord(
                index_code="000300.SH",
                con_code="000001.SZ",
                in_date="2024-03-31",
                membership_kind="snapshot",
            ),
            SectorMembershipRecord(
                index_code="000300.SH",
                con_code="000002.SZ",
                in_date="2024-03-31",
                membership_kind="snapshot",
            ),
            SectorMembershipRecord(
                index_code="000300.SH",
                con_code="000001.SZ",
                in_date="2024-04-30",
                membership_kind="snapshot",
            ),
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )


def test_pit_universe_uses_latest_membership_snapshot(tmp_path):
    database = tmp_path / "pit.sqlite3"
    _seed_universe(database)
    policy = {
        "index_codes": ["000300.SH"],
        "min_history_sessions": 1,
        "min_close": 0.0,
        "min_20d_amount": 0.0,
        "industry_mode": "zero",
    }

    april_15 = select_pit_universe(database, decision_date="2024-04-15", policy=policy)
    april_30 = select_pit_universe(database, decision_date="2024-04-30", policy=policy)

    assert april_15["index_snapshot_dates"]["000300.SH"] == "2024-03-31"
    assert april_15["eligible_codes"] == ["000001", "000002"]
    assert april_30["index_snapshot_dates"]["000300.SH"] == "2024-04-30"
    assert april_30["eligible_codes"] == ["000001"]
    assert april_30["industry_lookahead_safe"] is True


def test_pit_map_requires_industry_mapping():
    with pytest.raises(PitUniverseError, match="requires industry_by_code"):
        select_pit_universe(
            "unused.sqlite3",
            decision_date="2024-04-30",
            policy={"industry_mode": "pit_map"},
        )


def test_historical_policy_resolution_fails_when_policy_file_is_missing(tmp_path):
    with pytest.raises(PitUniverseError, match="historical.*PIT policy"):
        resolve_pit_policy(policy_path=tmp_path / "missing-policy.json")


def test_live_current_policy_is_explicitly_not_pit_ready():
    resolution = resolve_pit_policy(mode="live_current")

    assert resolution["policy"]["industry_mode"] == "current_proxy"
    assert resolution["industry_source"] == "current_metadata"
    assert resolution["industry_lookahead_safe"] is False
    assert resolution["pit_ready"] is False


def test_pit_membership_uses_as_of_industry_history(tmp_path):
    database = tmp_path / "pit-industry.sqlite3"
    _seed_universe(database)
    store = RevisionStore(database)
    ingest_records(
        store,
        "sector_membership",
        [
            SectorMembershipRecord(
                index_code="801010.SI",
                con_code="000001.SZ",
                in_date="2024-01-01",
                out_date="2024-05-01",
                industry_name="industry-a",
                membership_kind="interval",
            ),
            SectorMembershipRecord(
                index_code="801020.SI",
                con_code="000001.SZ",
                in_date="2024-05-01",
                industry_name="industry-b",
                membership_kind="interval",
            ),
            SectorMembershipRecord(
                index_code="801010.SI",
                con_code="000002.SZ",
                in_date="2024-01-01",
                industry_name="industry-a",
                membership_kind="interval",
            ),
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture-industry",
    )
    policy = {
        "index_codes": [],
        "min_history_sessions": 1,
        "min_close": 0.0,
        "min_20d_amount": 0.0,
        "require_decision_bar": False,
        "industry_mode": "pit_membership",
    }

    april = select_pit_universe(database, decision_date="2024-04-15", policy=policy)
    may = select_pit_universe(database, decision_date="2024-05-15", policy=policy)

    assert april["industry_by_code"]["000001"] == "industry-a"
    assert april["industry_by_code"]["000002"] == "industry-a"
    assert may["industry_by_code"]["000001"] == "industry-b"
    assert may["industry_by_code"]["000002"] == "industry-a"
    assert may["industry_lookahead_safe"] is True


def test_compact_context_matches_full_pit_selection(tmp_path):
    database = tmp_path / "pit-compact.sqlite3"
    _seed_universe(database)
    store = RevisionStore(database)
    policy = {
        "index_codes": ["000300.SH"],
        "min_history_sessions": 1,
        "min_close": 0.0,
        "min_20d_amount": 0.0,
        "industry_mode": "zero",
    }
    decision_date = "2024-04-30"

    full = select_pit_universe_from_context(
        build_pit_context(store),
        decision_date=decision_date,
        policy=policy,
    )
    compact_context = load_compact_history_pit_context(
        database,
        decision_dates=[decision_date],
        end_date=decision_date,
    )
    compact = select_pit_universe_from_context(
        compact_context,
        decision_date=decision_date,
        policy=policy,
    )

    assert compact["eligible_codes"] == full["eligible_codes"]
    assert compact["industry_by_code"] == full["industry_by_code"]


def test_pit_requires_valid_bar_on_decision_date(tmp_path):
    database = tmp_path / "pit-decision-bar.sqlite3"
    _seed_universe(database)

    selection = select_pit_universe(
        database,
        decision_date="2024-05-15",
        policy={
            "index_codes": ["000300.SH"],
            "min_history_sessions": 1,
            "min_close": 0.0,
            "min_20d_amount": 0.0,
            "industry_mode": "zero",
        },
    )

    assert selection["eligible_codes"] == []
    assert "missing_decision_bar" in selection["excluded"]["000001"]


def test_interval_membership_uses_half_open_date_window(tmp_path):
    database = tmp_path / "pit-interval.sqlite3"
    _seed_universe(database)
    store = RevisionStore(database)
    ingest_records(
        store,
        "sector_membership",
        [
            SectorMembershipRecord(
                index_code="399001.SZ",
                con_code="000001.SZ",
                in_date="2024-01-01",
                out_date="2024-05-01",
                membership_kind="interval",
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture-interval",
    )
    policy = {
        "index_codes": ["399001.SZ"],
        "min_history_sessions": 1,
        "min_close": 0.0,
        "min_20d_amount": 0.0,
        "require_decision_bar": False,
        "require_security_state": False,
        "industry_mode": "zero",
    }

    april_30 = select_pit_universe(database, decision_date="2024-04-30", policy=policy)
    may_1 = select_pit_universe(database, decision_date="2024-05-01", policy=policy)

    assert april_30["eligible_codes"] == ["000001"]
    assert april_30["index_snapshot_dates"]["399001.SZ"] == "2024-01-01"
    assert may_1["eligible_codes"] == []
    assert may_1["index_snapshot_dates"]["399001.SZ"] == ""


def test_pit_membership_fails_closed_on_missing_semantics(tmp_path):
    database = tmp_path / "pit-missing-semantics.sqlite3"
    _seed_universe(database)
    store = RevisionStore(database)
    ingest_records(
        store,
        "sector_membership",
        [
            SectorMembershipRecord(
                index_code="399002.SZ",
                con_code="000001.SZ",
                in_date="2024-01-01",
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture-missing-semantics",
    )

    with pytest.raises(PitUniverseError, match="missing membership_kind"):
        select_pit_universe(
            database,
            decision_date="2024-04-30",
            policy={
                "index_codes": ["399002.SZ"],
                "min_history_sessions": 1,
                "min_close": 0.0,
                "min_20d_amount": 0.0,
                "industry_mode": "zero",
            },
        )


def test_pit_membership_fails_closed_on_conflicting_semantics(tmp_path):
    database = tmp_path / "pit-conflicting-semantics.sqlite3"
    _seed_universe(database)
    store = RevisionStore(database)
    ingest_records(
        store,
        "sector_membership",
        [
            SectorMembershipRecord(
                index_code="399003.SZ",
                con_code="000001.SZ",
                in_date="2024-01-01",
                membership_kind="snapshot",
            ),
            SectorMembershipRecord(
                index_code="399003.SZ",
                con_code="000002.SZ",
                in_date="2024-01-01",
                membership_kind="interval",
            ),
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture-conflicting-semantics",
    )

    with pytest.raises(PitUniverseError, match="conflicting membership_kind"):
        select_pit_universe(
            database,
            decision_date="2024-04-30",
            policy={
                "index_codes": ["399003.SZ"],
                "min_history_sessions": 1,
                "min_close": 0.0,
                "min_20d_amount": 0.0,
                "industry_mode": "zero",
            },
        )
