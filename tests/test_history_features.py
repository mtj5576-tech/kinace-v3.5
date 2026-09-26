from __future__ import annotations

import json
import hashlib
from datetime import date, timedelta

import pytest

from kinace_v3.data_collection.ingest import ingest_records
from kinace_v3.data_collection.models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    DailyBasicMetricRecord,
    ForwardLabelRecord,
    RevisionStatus,
    SectorMembershipRecord,
    SecurityStateRecord,
    StockMetadataRecord,
)
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.history_features import HistoryFeatureError, build_history_snapshots
from kinace_v3.optimization import OptimizationError, run_parameter_optimization
from kinace_v3.pit_universe import DEFAULT_PIT_POLICY
from kinace_v3.portfolio_backtest import PortfolioBacktestError, discover_snapshots
from kinace_v3.research_audit import ResearchAuditError, build_snapshot_binding


def _pit_policy() -> dict:
    return {
        **DEFAULT_PIT_POLICY,
        "index_codes": [],
        "min_history_sessions": 20,
        "min_close": 0.0,
        "min_20d_amount": 0.0,
        "industry_mode": "pit_membership",
    }


def test_history_snapshots_are_built_for_each_eligible_real_date(tmp_path, monkeypatch):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    start = date(2026, 1, 1)
    bars: list[DailyBarRecord] = []
    basics: list[DailyBasicMetricRecord] = []
    labels: list[ForwardLabelRecord] = []
    states: list[SecurityStateRecord] = []
    for index in range(30):
        current_date = start + timedelta(days=index)
        close = 10.0 + index * 0.05
        bars.append(
            DailyBarRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                open=close - 0.05,
                high=close + 0.10,
                low=close - 0.10,
                close=close,
                vol=1000 + index,
                amount=10000 + index * 10,
            )
        )
        basics.append(
            DailyBasicMetricRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                close=close,
                turnover_rate=1.0 + index / 100.0,
            )
        )
        if index < 29:
            labels.append(
                ForwardLabelRecord(
                    ts_code="000001",
                    decision_date=current_date.isoformat(),
                    next_trade_date=(current_date + timedelta(days=1)).isoformat(),
                    next_open=close + 0.05,
                    next_close=close + 0.10,
                )
            )
        states.append(
            SecurityStateRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                is_suspended=False,
                is_limit_up=False,
                is_limit_down=False,
                prev_close=close,
            )
        )
    ingest_records(
        store,
        "stock_metadata",
        [
            StockMetadataRecord(
                ts_code="000001",
                name="Test",
                industry="bank",
                list_date="2020-01-01",
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(store, "daily_bars", bars, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(store, "daily_basic_metrics", basics, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(store, "forward_labels", labels, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(store, "security_state", states, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(
        store,
        "sector_membership",
        [
                SectorMembershipRecord(
                    index_code="000300.SH",
                    con_code="000001",
                    in_date="2025-12-31",
                    industry_name="bank",
                    membership_kind="interval",
                )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )

    def _unexpected_current_records(*args, **kwargs):
        raise AssertionError("history snapshot build must not materialize current_records")

    monkeypatch.setattr(RevisionStore, "current_records", _unexpected_current_records)

    with pytest.raises(HistoryFeatureError, match="PIT policy|pit-universe"):
        build_history_snapshots(
            store,
            database=store.database,
            output_dir=tmp_path / "snapshots-missing-policy",
            min_history=20,
            pit_policy_path=tmp_path / "missing-pit-policy.json",
        )
    assert not (tmp_path / "snapshots-missing-policy").exists()

    result = build_history_snapshots(
        store,
        database=store.database,
        output_dir=tmp_path / "snapshots",
        min_history=20,
        pit_policy=_pit_policy(),
    )

    assert result["snapshot_count"] == 11
    assert result["row_count"] == 11
    assert result["replay_ready_rows"] == 11
    assert result["industry_source"] == "pit_membership"
    assert result["industry_coverage"] == 1.0
    assert result["pit_ready"] is True
    assert result["industry_lookahead_safe"] is True
    assert len(result["pit_policy_hash"]) == 64
    manifest = json.loads(
        (tmp_path / "snapshots" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["pit_policy_hash"] == result["pit_policy_hash"]
    assert manifest["industry_source"] == "pit_membership"
    assert manifest["industry_coverage"] == 1.0
    assert manifest["pit_ready"] is True
    assert (tmp_path / "snapshots" / "manifest.json").is_file()
    first_snapshot = json.loads((tmp_path / "snapshots" / "2026-01-20.json").read_text(encoding="utf-8"))
    row = first_snapshot["rows"][0]
    assert row["raw_inputs"]["rel_sector_strength"] == 0.0
    assert row["labels"]["forward_return"] > 0
    assert row["replay_ready"] is True

    pit_result = build_history_snapshots(
        store,
        database=store.database,
        output_dir=tmp_path / "snapshots-pit",
        min_history=20,
        pit_policy=_pit_policy(),
    )
    pit_snapshot = json.loads(
        (tmp_path / "snapshots-pit" / "2026-01-20.json").read_text(encoding="utf-8")
    )

    assert pit_result["snapshot_count"] == 11
    assert pit_result["pit_policy"]["industry_mode"] == "pit_membership"
    assert pit_result["snapshots"][0]["universe"]["eligible_count"] == 1
    assert pit_snapshot["rows"][0]["raw_inputs"]["rel_sector_strength"] == 0.0
    assert (
        pit_snapshot["rows"][0]["raw_inputs"]["rel_sector_strength_source"]
        == "industry_mean"
    )


def test_history_snapshots_use_industry_as_of_each_decision_date(tmp_path):
    store = RevisionStore(tmp_path / "industry-switch.sqlite3")
    start = date(2026, 1, 1)
    bars: list[DailyBarRecord] = []
    basics: list[DailyBasicMetricRecord] = []
    states: list[SecurityStateRecord] = []
    for code, daily_return in (("000001", 0.02), ("000002", 0.01)):
        close = 10.0
        for index in range(30):
            current_date = start + timedelta(days=index)
            previous = close
            close = previous * (1.0 + daily_return)
            bars.append(
                DailyBarRecord(
                    ts_code=code,
                    trade_date=current_date.isoformat(),
                    open=previous,
                    high=close + 0.10,
                    low=min(previous, close) - 0.10,
                    close=close,
                    vol=1000 + index,
                    amount=10000 + index * 10,
                )
            )
            basics.append(
                DailyBasicMetricRecord(
                    ts_code=code,
                    trade_date=current_date.isoformat(),
                    close=close,
                    turnover_rate=1.0,
                )
            )
            states.append(
                SecurityStateRecord(
                    ts_code=code,
                    trade_date=current_date.isoformat(),
                    is_suspended=False,
                    is_limit_up=False,
                    is_limit_down=False,
                    prev_close=previous,
                )
            )
    ingest_records(
        store,
        "stock_metadata",
        [
            StockMetadataRecord(
                ts_code="000001",
                name="A",
                industry="current-a",
                list_date="2020-01-01",
            ),
            StockMetadataRecord(
                ts_code="000002",
                name="B",
                industry="current-b",
                list_date="2020-01-01",
            ),
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(store, "daily_bars", bars, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(store, "daily_basic_metrics", basics, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(store, "security_state", states, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(
        store,
        "sector_membership",
        [
                SectorMembershipRecord(
                    index_code="801010.SI",
                    con_code="000001",
                    in_date="2025-12-31",
                    out_date="2026-01-25",
                    industry_name="industry-a",
                    membership_kind="interval",
                ),
                SectorMembershipRecord(
                    index_code="801020.SI",
                    con_code="000001",
                    in_date="2026-01-25",
                    industry_name="industry-b",
                    membership_kind="interval",
                ),
                SectorMembershipRecord(
                    index_code="801010.SI",
                    con_code="000002",
                    in_date="2025-12-31",
                    industry_name="industry-a",
                    membership_kind="interval",
                ),
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture-industry",
    )

    result = build_history_snapshots(
        store,
        database=store.database,
        output_dir=tmp_path / "industry-switch",
        min_history=20,
        pit_policy=_pit_policy(),
    )

    early = json.loads(
        (tmp_path / "industry-switch" / "2026-01-20.json").read_text(encoding="utf-8")
    )
    late = json.loads(
        (tmp_path / "industry-switch" / "2026-01-25.json").read_text(encoding="utf-8")
    )
    early_by_code = {row["code"]: row for row in early["rows"]}
    late_by_code = {row["code"]: row for row in late["rows"]}
    assert early_by_code["000001"]["raw_inputs"]["rel_sector_strength"] > 0
    assert early_by_code["000002"]["raw_inputs"]["rel_sector_strength"] < 0
    assert late_by_code["000001"]["raw_inputs"]["rel_sector_strength"] == 0.0
    assert late_by_code["000002"]["raw_inputs"]["rel_sector_strength"] == 0.0
    assert result["pit_policy_hash"] == hashlib.sha256(
        json.dumps(
            result["pit_policy"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_live_current_snapshots_cannot_enter_historical_research(tmp_path):
    store = RevisionStore(tmp_path / "live-current.sqlite3")
    start = date(2026, 1, 1)
    bars = []
    basics = []
    states = []
    for index in range(25):
        current_date = start + timedelta(days=index)
        close = 10.0 + index * 0.01
        bars.append(
            DailyBarRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                open=close,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                vol=1000,
                amount=10000,
            )
        )
        basics.append(
            DailyBasicMetricRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                close=close,
                turnover_rate=1.0,
            )
        )
        states.append(
            SecurityStateRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                is_suspended=False,
                is_limit_up=False,
                is_limit_down=False,
                prev_close=close,
            )
        )
    ingest_records(
        store,
        "stock_metadata",
        [
            StockMetadataRecord(
                ts_code="000001",
                name="A",
                industry="current",
                list_date="2020-01-01",
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    ingest_records(store, "daily_bars", bars, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(store, "daily_basic_metrics", basics, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")
    ingest_records(store, "security_state", states, status=RevisionStatus.BACKFILLED_RELIABLE, source="fixture")

    result = build_history_snapshots(
        store,
        database=store.database,
        output_dir=tmp_path / "live-current",
        min_history=20,
        snapshot_mode="live_current",
    )

    assert result["industry_source"] == "current_metadata"
    assert result["industry_lookahead_safe"] is False
    assert result["pit_ready"] is False
    with pytest.raises(PortfolioBacktestError, match="pit_ready|lookahead|live_current"):
        discover_snapshots(tmp_path / "live-current")
    with pytest.raises(OptimizationError, match="pit_ready|lookahead|live_current"):
        run_parameter_optimization(
            tmp_path / "optimization.sqlite3",
            {"candidates": []},
            snapshot_dir=tmp_path / "live-current",
        )
    with pytest.raises(ResearchAuditError, match="pit_ready|lookahead|live_current"):
        build_snapshot_binding(tmp_path / "live-current" / "manifest.json")


def test_history_research_uses_total_return_series_and_pit_keeps_raw_min_close(
    tmp_path,
):
    store = RevisionStore(tmp_path / "price-basis.sqlite3")
    start = date(2026, 1, 1)
    dates = [start + timedelta(days=index) for index in range(25)]
    bars = []
    factors = []
    states = []
    for index, current_date in enumerate(dates):
        raw_close = 50.0 if index >= 20 else 100.0
        raw_open = raw_close - 0.10
        bars.append(
            DailyBarRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                open=raw_open,
                high=raw_close + 0.10,
                low=raw_open - 0.10,
                close=raw_close,
                pre_close=50.0 if index >= 20 else 100.0,
                vol=1000.0,
                amount=100_000.0,
            )
        )
        factors.append(
            AdjustmentFactorRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                adj_factor=2.0 if index >= 20 else 1.0,
                adjustment_basis_date=dates[-1].isoformat(),
            )
        )
        states.append(
            SecurityStateRecord(
                ts_code="000001",
                trade_date=current_date.isoformat(),
                is_suspended=False,
                is_limit_up=False,
                is_limit_down=False,
                prev_close=100.0,
            )
        )
    ingest_records(
        store,
        "stock_metadata",
        [
            StockMetadataRecord(
                ts_code="000001",
                name="A",
                industry="bank",
                list_date="2020-01-01",
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
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
        "adjustment_factors",
        factors,
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
                    index_code="801010.SI",
                    con_code="000001",
                    in_date="2025-12-31",
                    industry_name="bank",
                    membership_kind="interval",
                )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
    )
    base_policy = {
        **_pit_policy(),
        "min_close": 0.0,
    }

    research = build_history_snapshots(
        store,
        database=store.database,
        output_dir=tmp_path / "research-basis",
        min_history=20,
        pit_policy=base_policy,
        price_basis="total_return",
    )
    snapshot = json.loads(
        (tmp_path / "research-basis" / f"{dates[20].isoformat()}.json").read_text(
            encoding="utf-8"
        )
    )

    assert research["price_basis"] == "total_return"
    assert research["execution_price_basis"] == "raw"
    assert snapshot["price_basis"] == "total_return"
    assert snapshot["rows"][0]["price_basis"] == "total_return"
    assert snapshot["rows"][0]["raw_inputs"]["ret_5"] == pytest.approx(0.0)
    assert snapshot["rows"][0]["labels"]["price_basis"] == "total_return"

    raw_threshold = build_history_snapshots(
        store,
        database=store.database,
        output_dir=tmp_path / "raw-threshold",
        min_history=20,
        pit_policy={**base_policy, "min_close": 60.0},
        price_basis="total_return",
    )
    threshold_snapshot = json.loads(
        (tmp_path / "raw-threshold" / f"{dates[20].isoformat()}.json").read_text(
            encoding="utf-8"
        )
    )

    assert raw_threshold["execution_price_basis"] == "raw"
    assert threshold_snapshot["rows"] == []
