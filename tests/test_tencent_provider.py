from __future__ import annotations

from typing import Any, Mapping

from kinace_v3.data_collection.providers.tencent_provider import TencentMarketProvider


class FakeTransport:
    def __init__(self, rows):
        self.rows = rows
        self.urls: list[str] = []

    def get_json(self, url: str) -> Mapping[str, Any]:
        self.urls.append(url)
        symbol = url.split("param=", 1)[1].split(",", 1)[0]
        return {"data": {symbol: {"qfqday": self.rows}}}


def test_tencent_provider_derives_real_market_state_and_labels():
    transport = FakeTransport(
        [
            ["2026-09-11", "10.00", "10.20", "10.30", "9.90", "1000"],
            ["2026-09-12", "10.25", "10.50", "10.60", "10.10", "1200"],
            ["2026-09-15", "10.55", "10.70", "10.80", "10.40", "1300"],
        ]
    )
    provider = TencentMarketProvider(transport=transport, default_count=10)

    bundle = provider.fetch_history_bundle(["000001"])

    assert len(bundle.daily_bars) == 3
    assert len(bundle.security_state) == 3
    assert len(bundle.forward_labels) == 2
    assert bundle.daily_bars[0].ts_code == "000001"
    assert bundle.forward_labels[0].decision_date == "2026-09-11"
    assert bundle.forward_labels[0].next_trade_date == "2026-09-12"
    assert bundle.forward_labels[0].next_open == 10.25
    assert bundle.forward_labels[0].next_close == 10.50
    assert bundle.security_state[1].prev_close == 10.20
    assert bundle.security_state[1].is_limit_up is False
    assert transport.urls[0].endswith("sz000001,day,,,10,qfq")


def test_tencent_provider_supports_exchange_suffix_and_default_count():
    transport = FakeTransport(
        [["2026-09-15", "10.00", "10.20", "10.30", "9.90", "1000"]]
    )
    provider = TencentMarketProvider(transport=transport, default_count=42)

    rows = provider.fetch_daily_bars(ts_code="600000.SH")

    assert len(rows) == 1
    assert rows[0].ts_code == "600000"
    assert transport.urls[0].endswith("sh600000,day,,,42,qfq")
