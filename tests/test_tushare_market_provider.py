from __future__ import annotations

from kinace_v3.data_collection.models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    DailyBasicMetricRecord,
    TradeCalendarRecord,
)
from kinace_v3.data_collection.providers.tushare_market_provider import (
    TushareMarketProvider,
)
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.market_sync import sync_market


class FakeTushareProvider:
    def __init__(self) -> None:
        self.daily_calls: list[dict] = []
        self.factor_calls: list[dict] = []

    def fetch_daily_bars(self, **kwargs):
        self.daily_calls.append(kwargs)
        records = [
            DailyBarRecord(
                ts_code="000001.SZ",
                trade_date="2026-09-11",
                open=10.0,
                high=10.3,
                low=9.9,
                close=10.0,
                pre_close=9.8,
                change=0.2,
                pct_chg=2.04,
                vol=1000,
                amount=5000,
            ),
            DailyBarRecord(
                ts_code="000001.SZ",
                trade_date="2026-09-12",
                open=10.5,
                high=11.2,
                low=10.4,
                close=11.0,
                pre_close=10.0,
                change=1.0,
                pct_chg=10.0,
                vol=1200,
                amount=6000,
            ),
        ]
        return [
            record
            for record in records
            if (kwargs.get("start_date") is None or record.trade_date >= kwargs["start_date"])
            and (kwargs.get("end_date") is None or record.trade_date <= kwargs["end_date"])
        ]

    def fetch_adjustment_factors(self, **kwargs):
        self.factor_calls.append(kwargs)
        records = [
            AdjustmentFactorRecord(
                ts_code="000001.SZ",
                trade_date="2026-09-11",
                adj_factor=1.0,
            ),
            AdjustmentFactorRecord(
                ts_code="000001.SZ",
                trade_date="2026-09-12",
                adj_factor=1.1,
            ),
        ]
        return [
            record
            for record in records
            if (kwargs.get("start_date") is None or record.trade_date >= kwargs["start_date"])
            and (kwargs.get("end_date") is None or record.trade_date <= kwargs["end_date"])
        ]


class DateWindowFakeTushareProvider:
    def __init__(self) -> None:
        self.calendar_calls: list[dict] = []
        self.daily_calls: list[dict] = []
        self.factor_calls: list[dict] = []

    def trade_cal(self, **kwargs):
        self.calendar_calls.append(kwargs)
        return [
            TradeCalendarRecord(
                exchange="SSE",
                cal_date=kwargs["start_date"],
                is_open=True,
            ),
            TradeCalendarRecord(
                exchange="SSE",
                cal_date=kwargs["end_date"],
                is_open=True,
            ),
        ]

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        trade_date = kwargs["trade_date"]
        return [
            DailyBarRecord(
                ts_code=f"00000{index}.SZ",
                trade_date=trade_date,
                open=10.0,
                high=10.3,
                low=9.9,
                close=10.1,
                pre_close=10.0,
                change=0.1,
                pct_chg=1.0,
                vol=1000,
                amount=5000,
            )
            for index in range(1, 9)
        ]

    def adj_factor(self, **kwargs):
        self.factor_calls.append(kwargs)
        trade_date = kwargs["trade_date"]
        return [
            AdjustmentFactorRecord(
                ts_code=f"00000{index}.SZ",
                trade_date=trade_date,
                adj_factor=1.0,
            )
            for index in range(1, 9)
        ]


def test_tushare_market_provider_keeps_raw_prices_and_separate_factors():
    low_level = FakeTushareProvider()
    provider = TushareMarketProvider(provider=low_level)

    bundle = provider.fetch_history_bundle(
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
    )

    assert low_level.daily_calls[0]["ts_code"] == "000001.SZ"
    assert low_level.factor_calls[0]["ts_code"] == "000001.SZ"
    assert [row.close for row in bundle.daily_bars] == [10.0, 11.0]
    assert [row.pre_close for row in bundle.daily_bars] == [9.8, 10.0]
    assert bundle.daily_bars[0].vol == 100000.0
    assert bundle.daily_bars[0].amount == 5_000_000.0
    assert bundle.security_state[0].up_limit == 10.78
    assert bundle.security_state[0].down_limit == 8.82
    assert bundle.security_state[1].is_limit_up is True
    assert bundle.forward_labels[0].next_open == 10.5
    assert bundle.daily_basic_metrics[0].volume_ratio == 1.0
    assert [row.adj_factor for row in bundle.adjustment_factors] == [1.0, 1.1]
    assert {
        row.adjustment_basis_date for row in bundle.adjustment_factors
    } == {"2026-09-12"}


def test_tushare_market_provider_extension_preserves_existing_raw_window():
    provider = TushareMarketProvider(provider=FakeTushareProvider())

    first = provider.fetch_history_bundle(
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-11",
    )
    extended = provider.fetch_history_bundle(
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
    )

    assert extended.daily_bars[0].to_dict() == first.daily_bars[0].to_dict()
    assert extended.security_state[0].to_dict() == first.security_state[0].to_dict()
    assert first.daily_bars[0].close == 10.0
    assert first.adjustment_factors[0].adjustment_basis_date == "2026-09-11"
    assert extended.adjustment_factors[0].trade_date == "2026-09-11"
    assert extended.adjustment_factors[0].adjustment_basis_date == "2026-09-12"


def test_tushare_market_provider_uses_trade_date_window_for_large_code_sets():
    low_level = DateWindowFakeTushareProvider()
    provider = TushareMarketProvider(provider=low_level)
    codes = [f"00000{index}" for index in range(1, 9)]

    bundle = provider.fetch_history_bundle(
        codes,
        start_date="2026-09-11",
        end_date="2026-09-12",
    )

    assert [call["trade_date"] for call in low_level.daily_calls] == [
        "2026-09-11",
        "2026-09-12",
    ]
    assert low_level.calendar_calls[0]["exchange"] == "SSE"
    assert all("ts_code" not in call for call in low_level.daily_calls)
    assert len(bundle.daily_bars) == 16
    assert {row.ts_code for row in bundle.daily_bars} == {
        f"00000{index}" for index in range(1, 9)
    }


def test_sync_market_accepts_tushare_provider(tmp_path):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    provider = TushareMarketProvider(provider=FakeTushareProvider())

    result = sync_market(
        store,
        provider,
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
        parity_output=tmp_path / "parity.json",
    )

    assert result["provider"] == "tushare_daily_adj"
    assert result["price_basis"] == "raw"
    assert result["parity"]["passed"] is True
    assert result["adjustment_basis_dates_by_code"] == {"000001": "2026-09-12"}
    latest = store.current_revision(
        "daily_bars", {"ts_code": "000001", "trade_date": "2026-09-12"}
    )
    assert latest is not None
    assert latest["record"]["open"] == 10.5
    assert latest["record"]["close"] == 11.0
    assert latest["record"]["pre_close"] == 10.0
    factors = store.current_records("adjustment_factors")
    assert len(factors) == 2
    assert {
        row["record"]["adjustment_basis_date"] for row in factors
    } == {"2026-09-12"}


def test_sync_market_keeps_tushare_raw_prices_when_rebase_requested(tmp_path):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    provider = TushareMarketProvider(provider=FakeTushareProvider())

    result = sync_market(
        store,
        provider,
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
        parity_output=tmp_path / "parity.json",
        rebase_price_basis=True,
    )

    assert result["price_scale_by_code"] == {}
    assert result["volume_scale_by_code"] == {}
    assert result["rebased"] == {}
    latest = store.current_revision(
        "daily_bars", {"ts_code": "000001", "trade_date": "2026-09-12"}
    )
    previous = store.current_revision(
        "daily_bars", {"ts_code": "000001", "trade_date": "2026-09-11"}
    )
    assert latest is not None
    assert previous is not None
    assert latest["record"]["close"] == 11.0
    assert previous["record"]["close"] == 10.0


def test_sync_market_latest_only_skips_full_parity_scan(tmp_path):
    store = RevisionStore(tmp_path / "v32.sqlite3")
    provider = TushareMarketProvider(provider=FakeTushareProvider())
    parity_path = tmp_path / "parity.json"

    result = sync_market(
        store,
        provider,
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
        parity_output=parity_path,
        latest_only=True,
    )

    assert result["latest_only"] is True
    assert result["parity"]["status"] == "skipped"
    assert result["parity"]["overlap_records"] == 0
    assert parity_path.exists() is False
    assert store.current_revision(
        "daily_bars", {"ts_code": "000001", "trade_date": "2026-09-12"}
    )

    revision_count = store.count_revisions()
    repeated = sync_market(
        store,
        provider,
        ["000001"],
        start_date="2026-09-11",
        end_date="2026-09-12",
        latest_only=True,
    )
    assert repeated["ingested"]["daily_bars"]["inserted_count"] == 0
    assert store.count_revisions() == revision_count
