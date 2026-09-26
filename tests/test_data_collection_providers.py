from __future__ import annotations

import json

import pytest

from kinace_v3.data_collection.models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    DailyBasicMetricRecord,
    NameChangeIntervalRecord,
    SectorMembershipRecord,
    StockMetadataRecord,
    TradeCalendarRecord,
)
from kinace_v3.data_collection.providers import (
    CsvProvider,
    Provider,
    ProviderAPIError,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderRateLimitError,
    ProviderTransportError,
    TushareProProvider,
)


def _ok(fields, *items, code=0, msg=None):
    return {
        "code": code,
        "msg": msg,
        "data": {"fields": list(fields), "items": [list(item) for item in items]},
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, url, payload, *, timeout, headers):
        self.requests.append(
            {
                "url": url,
                "payload": payload,
                "timeout": timeout,
                "headers": headers,
            }
        )
        response = self.responses[payload["api_name"]]
        if isinstance(response, Exception):
            raise response
        return response


def test_csv_provider_is_deterministic_and_structurally_matches_protocol(tmp_path):
    path = tmp_path / "daily.csv"
    path.write_text(
        "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount\n"
        "000002.SZ,20240103,11,12,10,11.5,11,0.5,4.55,200,2200\n"
        "000001.SZ,20240102,10,11,9,10.5,9.8,0.7,7.14,100,1000\n",
        encoding="utf-8",
    )
    provider = CsvProvider(tmp_path)

    assert isinstance(provider, Provider)
    first = provider.fetch_daily_bars()
    second = provider.daily()
    assert first == second
    assert [type(record) for record in first] == [DailyBarRecord, DailyBarRecord]
    assert [record.ts_code for record in first] == ["000001.SZ", "000002.SZ"]
    assert first[0].to_dict() == {
        "ts_code": "000001.SZ",
        "trade_date": "2024-01-02",
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "pre_close": 9.8,
        "change": 0.7,
        "pct_chg": 7.14,
        "vol": 100.0,
        "amount": 1000.0,
    }


def test_csv_provider_loads_json_wrappers_and_validates_columns_and_dates(tmp_path):
    json_path = tmp_path / "daily_basic.json"
    json_path.write_text(
        json.dumps(
            {
                "data": {
                    "fields": ["ts_code", "trade_date", "turnover_rate", "pe"],
                    "items": [["000001.SZ", "20240102", "1.25", "8.5"]],
                }
            }
        ),
        encoding="utf-8",
    )
    provider = CsvProvider(files={"daily_basic": json_path})
    records = provider.fetch_daily_basic_metrics()
    assert len(records) == 1
    assert isinstance(records[0], DailyBasicMetricRecord)
    assert records[0].turnover_rate == 1.25
    assert records[0].pe == 8.5

    missing_path = tmp_path / "missing.csv"
    missing_path.write_text("ts_code,trade_date\n000001.SZ,20240102\n", encoding="utf-8")
    with pytest.raises(ProviderDataError, match="missing required columns"):
        CsvProvider(files={"daily_bars": missing_path}).load("daily_bars")

    invalid_path = tmp_path / "invalid-date.csv"
    invalid_path.write_text(
        "ts_code,trade_date,open,high,low,close\n000001.SZ,not-a-date,1,2,0.5,1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ProviderDataError, match="trade_date"):
        CsvProvider(files={"daily_bars": invalid_path}).load("daily_bars")


def test_tushare_methods_normalize_all_supported_datasets(monkeypatch):
    transport = FakeTransport(
        {
            "trade_cal": _ok(
                ["exchange", "cal_date", "is_open", "pretrade_date"],
                ["SSE", "20240102", "1", ""],
            ),
            "stock_basic": _ok(
                ["ts_code", "symbol", "name", "industry", "list_date"],
                ["000001.SZ", "000001", "PAYH", "Bank", "19910403"],
            ),
            "daily": _ok(
                ["ts_code", "trade_date", "open", "high", "low", "close"],
                ["000001.SZ", "20240102", "10.1", "10.5", "9.9", "10.4"],
            ),
            "daily_basic": _ok(
                ["ts_code", "trade_date", "turnover_rate", "total_mv"],
                ["000001.SZ", "20240102", "1.2", "12345"],
            ),
            "adj_factor": _ok(
                ["ts_code", "trade_date", "adj_factor"],
                ["000001.SZ", "20240102", "1.5"],
            ),
            "namechange": _ok(
                ["ts_code", "name", "start_date", "end_date", "ann_date"],
                ["000001.SZ", "PAYH", "20240102", "", "20240101"],
            ),
            "index_weight": _ok(
                ["index_code", "con_code", "trade_date", "weight"],
                ["000300.SH", "000001.SZ", "20240102", "1.5"],
            ),
            "index_member": _ok(
                ["index_code", "con_code", "in_date", "out_date"],
                ["000300.SH", "000001.SZ", "20240102", "20250102"],
            ),
        }
    )
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    provider = TushareProProvider(transport=transport, max_retries=0)

    calls = [
        ("trade_calendar", provider.trade_cal(exchange="SSE")),
        ("stock_metadata", provider.stock_basic(ts_code="000001.SZ")),
        ("daily_bars", provider.daily(trade_date="2024-01-02")),
        ("daily_basic_metrics", provider.daily_basic(trade_date="20240102")),
        ("adjustment_factors", provider.adj_factor(trade_date="20240102")),
        ("name_change_intervals", provider.namechange(ts_code="000001.SZ")),
        ("sector_membership", provider.index_weight(index_code="000300.SH")),
        ("sector_membership", provider.index_member(index_code="000300.SH")),
    ]
    expected_types = [
        TradeCalendarRecord,
        StockMetadataRecord,
        DailyBarRecord,
        DailyBasicMetricRecord,
        AdjustmentFactorRecord,
        NameChangeIntervalRecord,
        SectorMembershipRecord,
        SectorMembershipRecord,
    ]
    for (dataset, records), expected_type in zip(calls, expected_types):
        assert len(records) == 1
        assert isinstance(records[0], expected_type)
        assert records[0].dataset_name == dataset

    assert calls[-2][1][0].membership_kind == "snapshot"
    assert calls[-1][1][0].membership_kind == "interval"
    assert calls[-1][1][0].out_date == "2025-01-02"

    assert all(request["payload"]["token"] == "test-token" for request in transport.requests)
    assert all(request["url"] == "https://api.tushare.pro" for request in transport.requests)
    daily_request = next(
        request for request in transport.requests if request["payload"]["api_name"] == "daily"
    )
    assert daily_request["payload"]["params"]["trade_date"] == "20240102"


def test_tushare_transport_and_api_errors_are_typed_and_redact_token(monkeypatch):
    token = "super-secret-token"
    transport = FakeTransport({"daily": RuntimeError(f"request failed for {token}")})
    monkeypatch.setenv("TUSHARE_TOKEN", token)
    provider = TushareProProvider(transport=transport, max_retries=0)

    with pytest.raises(ProviderTransportError) as caught:
        provider.daily(trade_date="20240102")
    assert token not in str(caught.value)
    assert token not in repr(provider)
    assert token not in repr(caught.value)

    api_transport = FakeTransport(
        {"daily": {"code": -2001, "msg": f"permission denied for {token}"}}
    )
    api_provider = TushareProProvider(transport=api_transport, max_retries=0)
    with pytest.raises(ProviderAPIError) as api_error:
        api_provider.daily(trade_date="20240102")
    assert token not in str(api_error.value)
    assert "[REDACTED]" in str(api_error.value)


def test_tushare_transport_errors_retry_within_budget(monkeypatch):
    class FlakyTransport:
        def __init__(self):
            self.calls = 0

        def __call__(self, url, payload, *, timeout, headers):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary read timeout")
            return _ok(
                ("ts_code", "trade_date", "open", "high", "low", "close"),
                ("000001.SZ", "20240102", 10.0, 10.8, 9.9, 10.5),
            )

    transport = FlakyTransport()
    monkeypatch.setenv("TUSHARE_TOKEN", "retry-token")
    provider = TushareProProvider(
        transport=transport,
        max_retries=1,
        retry_backoff=0,
    )

    rows = provider.daily(trade_date="20240102")

    assert len(rows) == 1
    assert rows[0].close == 10.5
    assert transport.calls == 2


def test_tushare_rate_limit_retries_are_bounded_and_typed(monkeypatch):
    transport = FakeTransport(
        {
            "daily": {
                "code": -2002,
                "msg": "\u6bcf\u5206\u949f\u8bbf\u95ee\u6b21\u6570\u8fc7\u591a",
            }
        }
    )
    monkeypatch.setenv("TUSHARE_TOKEN", "rate-token")
    provider = TushareProProvider(
        transport=transport,
        max_retries=1,
        retry_backoff=0,
    )

    with pytest.raises(ProviderRateLimitError, match="rate limit"):
        provider.daily(trade_date="20240102")
    assert len(transport.requests) == 2


def test_tushare_requires_environment_token_and_never_serializes_it(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(ProviderConfigurationError, match="TUSHARE_TOKEN"):
        TushareProProvider(token=" ")
