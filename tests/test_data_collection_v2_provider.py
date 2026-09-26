from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from kinace_v3.data_collection.providers.v2_bootstrap_provider import (
    V2BootstrapProvider,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_v2_fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE daily_bars (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL NOT NULL,
                turnover REAL,
                pct REAL,
                industry TEXT,
                name TEXT,
                PRIMARY KEY (code, trade_date)
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO daily_bars (
                code, trade_date, open, high, low, close, volume, amount,
                turnover, pct, industry, name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", "2024-01-02", 10.0, 10.5, 9.9, 10.4, 1000, 10400, 1.2, 0.04, "银行", "平安银行"),
                ("000001", "2024-01-03", 10.4, 10.8, 10.2, 10.7, 1200, 12840, 1.4, 0.03, "银行", "平安银行"),
                ("600000", "2024-01-02", 8.0, 8.2, 7.9, 8.1, 900, 7290, 0.8, 0.01, "银行", "浦发银行"),
            ],
        )


def test_v2_bootstrap_provider_reads_daily_bars_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "kinace_v2_1.sqlite3"
    _create_v2_fixture(database)
    before = _sha256(database)

    provider = V2BootstrapProvider(database)
    records = provider.fetch_daily_bars(ts_code="000001")

    assert [record.trade_date for record in records] == ["2024-01-02", "2024-01-03"]
    assert records[0].ts_code == "000001"
    assert records[0].open == 10.0
    assert records[0].close == 10.4
    assert records[0].vol == 1000
    assert records[0].amount == 10400
    assert records[0].pct_chg == 0.04
    assert _sha256(database) == before


def test_v2_bootstrap_provider_exposes_calendar_metadata_and_basic_metrics(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kinace_v2_1.sqlite3"
    _create_v2_fixture(database)
    provider = V2BootstrapProvider(database)

    calendar = provider.fetch_trade_calendar()
    metadata = provider.fetch_stock_metadata()
    basic = provider.fetch_daily_basic_metrics(ts_code="000001")

    assert [record.cal_date for record in calendar] == ["2024-01-02", "2024-01-03"]
    assert calendar[1].pretrade_date == "2024-01-02"
    assert {record.ts_code for record in metadata} == {"000001", "600000"}
    assert len(basic) == 2
    assert basic[0].turnover_rate == 1.2
    assert provider.fetch_adjustment_factors() == ()
    assert provider.fetch_name_change_intervals() == ()
    assert provider.fetch_sector_membership() == ()
    states = provider.fetch_security_state(ts_code="000001")
    labels = provider.fetch_forward_labels(ts_code="000001")
    assert len(states) == 2
    assert states[1].prev_close == 10.4
    assert states[1].is_suspended is False
    assert len(labels) == 1
    assert labels[0].decision_date == "2024-01-02"
    assert labels[0].next_trade_date == "2024-01-03"
    assert labels[0].next_open == 10.4
    assert labels[0].next_close == 10.7
    assert labels[0].forward_return == pytest.approx(10.7 / 10.4 - 1.0)


def test_v2_bootstrap_provider_supports_date_filters(tmp_path: Path) -> None:
    database = tmp_path / "kinace_v2_1.sqlite3"
    _create_v2_fixture(database)
    provider = V2BootstrapProvider(database)

    records = provider.fetch_daily_bars(start_date="2024-01-03", end_date="2024-01-03")

    assert len(records) == 1
    assert records[0].trade_date == "2024-01-03"
    assert records[0].ts_code == "000001"
