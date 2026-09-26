from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from kinace_v3.data_collection.ingest import ingest_records
from kinace_v3.data_collection.models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    RevisionStatus,
)
from kinace_v3.data_collection.providers.tushare_market_provider import (
    TushareMarketProvider,
)
from kinace_v3.market_sync import MarketSyncError, sync_market, sync_tencent_market
from kinace_v3.data_collection.providers.tencent_provider import TencentMarketProvider
from kinace_v3.data_collection.store import RevisionStore


class FakeTransport:
    def __init__(self, rows):
        self.rows = rows

    def get_json(self, url: str) -> Mapping[str, Any]:
        symbol = url.split("param=", 1)[1].split(",", 1)[0]
        return {"data": {symbol: {"qfqday": self.rows}}}


def _provider(rows) -> TencentMarketProvider:
    return TencentMarketProvider(transport=FakeTransport(rows), default_count=10)


class CorporateActionTushareSource:
    def __init__(self) -> None:
        self.bars = [
            DailyBarRecord(
                ts_code="000001.SZ",
                trade_date="2024-06-07",
                open=9.8,
                high=10.2,
                low=9.7,
                close=10.0,
                pre_close=9.9,
                vol=1000,
                amount=5000,
            ),
            DailyBarRecord(
                ts_code="000001.SZ",
                trade_date="2024-06-10",
                open=10.0,
                high=10.3,
                low=9.9,
                close=10.2,
                pre_close=10.0,
                vol=1100,
                amount=5500,
            ),
            DailyBarRecord(
                ts_code="000001.SZ",
                trade_date="2024-06-11",
                open=5.1,
                high=5.2,
                low=5.0,
                close=5.1,
                pre_close=5.05,
                vol=2200,
                amount=5500,
            ),
        ]
        self.factors = [
            AdjustmentFactorRecord(
                ts_code="000001.SZ",
                trade_date="2024-06-07",
                adj_factor=1.0,
            ),
            AdjustmentFactorRecord(
                ts_code="000001.SZ",
                trade_date="2024-06-10",
                adj_factor=1.0,
            ),
            AdjustmentFactorRecord(
                ts_code="000001.SZ",
                trade_date="2024-06-11",
                adj_factor=2.0,
            ),
        ]

    @staticmethod
    def _window(records, start_date, end_date):
        return [
            record
            for record in records
            if start_date <= record.trade_date <= end_date
        ]

    def fetch_daily_bars(self, **kwargs):
        return self._window(
            self.bars,
            kwargs["start_date"],
            kwargs["end_date"],
        )

    def fetch_adjustment_factors(self, **kwargs):
        return self._window(
            self.factors,
            kwargs["start_date"],
            kwargs["end_date"],
        )


def test_market_sync_checks_overlap_then_ingests_real_labels_and_state(tmp_path):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    existing = DailyBarRecord(
        ts_code="000001",
        trade_date="2026-09-11",
        open=10.0,
        high=10.3,
        low=9.9,
        close=10.2,
        vol=100000,
        amount=10000,
    )
    ingest_records(
        store,
        "daily_bars",
        [existing],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="v2-history",
    )
    rows = [
        ["2026-09-11", "10.00", "10.20", "10.30", "9.90", "1000"],
        ["2026-09-12", "10.25", "10.50", "10.60", "10.10", "1200"],
    ]
    parity_path = tmp_path / "parity.json"

    result = sync_tencent_market(
        store,
        _provider(rows),
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
        parity_output=parity_path,
    )

    assert result["parity"]["overlap_records"] == 1
    assert result["parity"]["mismatch_records"] == 0
    assert result["parity"]["passed"] is True
    assert json.loads(parity_path.read_text(encoding="utf-8"))["passed"] is True
    assert result["ingested"]["forward_labels"]["inserted_count"] == 1
    assert store.current_revision("forward_labels", {"ts_code": "000001", "decision_date": "2026-09-11"})


def test_market_sync_rejects_overlap_mismatch_without_writing(tmp_path):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    ingest_records(
        store,
        "daily_bars",
        [
            DailyBarRecord(
                ts_code="000001",
                trade_date="2026-09-11",
                open=10.0,
                high=10.3,
                low=9.9,
                close=10.2,
                vol=1000,
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="v2-history",
    )
    rows = [
        ["2026-09-11", "10.00", "10.90", "10.95", "9.90", "1000"],
        ["2026-09-12", "10.25", "10.50", "10.60", "10.10", "1200"],
    ]

    with pytest.raises(MarketSyncError, match="source parity failed"):
        sync_tencent_market(
            store,
            _provider(rows),
            ["000001"],
            start_date="2026-09-11",
            end_date="2026-09-12",
            parity_output=tmp_path / "parity.json",
        )

    assert store.current_revision(
        "daily_bars", {"ts_code": "000001", "trade_date": "2026-09-12"}
    ) is None


def test_market_sync_skips_sparse_prior_date_return_parity(tmp_path):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    ingest_records(
        store,
        "daily_bars",
        [
            DailyBarRecord(
                ts_code="000001",
                trade_date="2026-09-12",
                open=10.25,
                high=10.60,
                low=10.10,
                close=10.50,
                vol=1200,
            )
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="sparse-history",
    )
    rows = [
        ["2026-09-11", "10.00", "10.20", "10.30", "9.90", "1000"],
        ["2026-09-12", "10.25", "10.50", "10.60", "10.10", "1200"],
    ]

    result = sync_tencent_market(
        store,
        _provider(rows),
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
        parity_output=tmp_path / "parity.json",
    )

    assert result["parity"]["overlap_records"] == 1
    assert result["parity"]["mismatch_records"] == 0
    assert result["parity"]["passed"] is True


def test_tushare_market_sync_extension_keeps_raw_execution_inputs(tmp_path):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    provider = TushareMarketProvider(provider=CorporateActionTushareSource())

    first = sync_market(
        store,
        provider,
        ["000001"],
        start_date="2024-06-07",
        end_date="2024-06-10",
        parity_output=tmp_path / "first-parity.json",
    )
    before_bar = store.current_revision(
        "daily_bars", {"ts_code": "000001", "trade_date": "2024-06-10"}
    )
    before_state = store.current_revision(
        "security_state", {"ts_code": "000001", "trade_date": "2024-06-10"}
    )
    assert before_bar is not None
    assert before_state is not None
    assert before_bar["record"]["close"] == 10.2
    assert before_state["record"]["prev_close"] == 10.0
    assert before_state["record"]["up_limit"] == 11.0
    assert first["adjustment_basis_dates_by_code"] == {"000001": "2024-06-10"}

    extended = sync_market(
        store,
        provider,
        ["000001"],
        start_date="2024-06-07",
        end_date="2024-06-11",
        parity_output=tmp_path / "extended-parity.json",
    )

    after_bar = store.current_revision(
        "daily_bars", {"ts_code": "000001", "trade_date": "2024-06-10"}
    )
    after_state = store.current_revision(
        "security_state", {"ts_code": "000001", "trade_date": "2024-06-10"}
    )
    assert after_bar == before_bar
    assert after_state == before_state
    assert after_bar["record"]["open"] == 10.0
    assert after_bar["record"]["high"] == 10.3
    assert after_bar["record"]["low"] == 9.9
    assert after_bar["record"]["close"] == 10.2
    assert after_bar["record"]["pre_close"] == 10.0
    assert after_bar["record"]["vol"] == 110000.0
    assert after_bar["record"]["amount"] == 5_500_000.0
    assert extended["adjustment_basis_dates_by_code"] == {"000001": "2024-06-11"}
    old_factor = store.current_revision(
        "adjustment_factors",
        {
            "ts_code": "000001",
            "trade_date": "2024-06-10",
        },
    )
    new_factor = store.current_revision(
        "adjustment_factors",
        {
            "ts_code": "000001",
            "trade_date": "2024-06-11",
        },
    )
    assert old_factor["record"]["adjustment_basis_date"] == "2024-06-10"
    assert new_factor["record"]["adj_factor"] == 2.0
    assert new_factor["record"]["adjustment_basis_date"] == "2024-06-11"
