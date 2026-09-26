"""Read-only bootstrap provider backed by the Kinace V2.1 SQLite database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from ..models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    DailyBasicMetricRecord,
    ForwardLabelRecord,
    NameChangeIntervalRecord,
    SectorMembershipRecord,
    SecurityStateRecord,
    StockMetadataRecord,
    TradeCalendarRecord,
)
from .base import ProviderConfigurationError, ProviderDataError, sort_records


DEFAULT_V2_DATABASE = Path(r"D:\kinacesql\kinace_v2_1.sqlite3")


def _read_only_uri(path: Path) -> str:
    normalized = path.resolve(strict=False).as_posix()
    return f"file:{quote(normalized, safe='/:')}?mode=ro&immutable=1"


def _limit_pct(code: str, name: str = "") -> float:
    if "ST" in name.upper() or "退" in name:
        return 0.05
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


def _round_price(value: float) -> float:
    return int(value * 100 + 0.5) / 100.0


class V2BootstrapProvider:
    """Expose V2.1 observations through the provider protocol without writes."""

    source_name = "kinace_v2_1_bootstrap"

    def __init__(self, database: str | Path = DEFAULT_V2_DATABASE) -> None:
        self.database = Path(database).resolve(strict=False)
        if not self.database.is_file():
            raise ProviderConfigurationError(
                f"V2.1 bootstrap database does not exist: {self.database}"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(_read_only_uri(self.database), uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def fetch_daily_bars(
        self,
        *,
        ts_code: str | None = None,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> Sequence[DailyBarRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if ts_code:
            clauses.append("code = ?")
            params.append(ts_code)
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT code, trade_date, open, high, low, close, volume, amount, pct
            FROM daily_bars
            {where}
            ORDER BY code, trade_date
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise ProviderDataError(f"V2.1 daily_bars read failed: {exc}") from exc
        records = [
            DailyBarRecord(
                ts_code=str(row["code"]),
                trade_date=str(row["trade_date"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                pct_chg=row["pct"],
                vol=row["volume"],
                amount=row["amount"],
            )
            for row in rows
        ]
        return sort_records(records)

    def fetch_trade_calendar(
        self,
        *,
        exchange: str = "SSE",
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> Sequence[TradeCalendarRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT DISTINCT trade_date FROM daily_bars {where} ORDER BY trade_date"
        try:
            with self._connect() as connection:
                dates = [str(row[0]) for row in connection.execute(sql, params)]
        except sqlite3.Error as exc:
            raise ProviderDataError(f"V2.1 trade calendar read failed: {exc}") from exc
        records = [
            TradeCalendarRecord(
                exchange=exchange,
                cal_date=date,
                is_open=True,
                pretrade_date=dates[index - 1] if index else None,
            )
            for index, date in enumerate(dates)
        ]
        return sort_records(records)

    def fetch_stock_metadata(self, **_: Any) -> Sequence[StockMetadataRecord]:
        sql = """
            SELECT code,
                   COALESCE(MAX(name), code) AS name,
                   MAX(industry) AS industry
            FROM daily_bars
            GROUP BY code
            ORDER BY code
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(sql).fetchall()
        except sqlite3.Error as exc:
            raise ProviderDataError(f"V2.1 stock metadata read failed: {exc}") from exc
        return sort_records(
            [
                StockMetadataRecord(
                    ts_code=str(row["code"]),
                    name=str(row["name"]),
                    industry=row["industry"],
                )
                for row in rows
            ]
        )

    def fetch_daily_basic_metrics(
        self,
        *,
        ts_code: str | None = None,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> Sequence[DailyBasicMetricRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if ts_code:
            clauses.append("code = ?")
            params.append(ts_code)
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT code, trade_date, close, turnover
            FROM daily_bars
            {where}
            ORDER BY code, trade_date
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise ProviderDataError(f"V2.1 daily basic read failed: {exc}") from exc
        return sort_records(
            [
                DailyBasicMetricRecord(
                    ts_code=str(row["code"]),
                    trade_date=str(row["trade_date"]),
                    close=row["close"],
                    turnover_rate=row["turnover"],
                )
                for row in rows
            ]
        )

    def fetch_adjustment_factors(self, **_: Any) -> Sequence[AdjustmentFactorRecord]:
        return ()

    def fetch_name_change_intervals(
        self, **_: Any
    ) -> Sequence[NameChangeIntervalRecord]:
        return ()

    def fetch_sector_membership(self, **_: Any) -> Sequence[SectorMembershipRecord]:
        return ()

    def fetch_security_state(
        self,
        *,
        ts_code: str | None = None,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> Sequence[SecurityStateRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if ts_code:
            clauses.append("code = ?")
            params.append(ts_code)
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT code, trade_date, close, volume, name
            FROM daily_bars
            {where}
            ORDER BY code, trade_date
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise ProviderDataError(f"V2.1 security state read failed: {exc}") from exc

        previous_close: dict[str, float] = {}
        states: list[SecurityStateRecord] = []
        for row in rows:
            code = str(row["code"])
            close = float(row["close"] or 0)
            prior = previous_close.get(code)
            if close > 0:
                previous_close[code] = close
            limit = _limit_pct(code, str(row["name"] or ""))
            up_limit = _round_price(prior * (1 + limit)) if prior and prior > 0 else None
            down_limit = _round_price(prior * (1 - limit)) if prior and prior > 0 else None
            suspended = float(row["volume"] or 0) <= 0
            states.append(
                SecurityStateRecord(
                    ts_code=code,
                    trade_date=str(row["trade_date"]),
                    is_suspended=suspended,
                    is_limit_up=bool(
                        not suspended and up_limit is not None and close >= up_limit - 0.001
                    ),
                    is_limit_down=bool(
                        not suspended and down_limit is not None and close <= down_limit + 0.001
                    ),
                    prev_close=prior,
                    up_limit=up_limit,
                    down_limit=down_limit,
                )
            )
        return sort_records(states)

    def fetch_forward_labels(
        self,
        *,
        ts_code: str | None = None,
        decision_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> Sequence[ForwardLabelRecord]:
        rows = self.fetch_daily_bars(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )
        by_code: dict[str, list[DailyBarRecord]] = {}
        for row in rows:
            by_code.setdefault(row.ts_code, []).append(row)
        labels: list[ForwardLabelRecord] = []
        for code, code_rows in by_code.items():
            ordered = sorted(code_rows, key=lambda item: item.trade_date)
            for current, following in zip(ordered, ordered[1:]):
                if decision_date and current.trade_date != decision_date:
                    continue
                labels.append(
                    ForwardLabelRecord(
                        ts_code=code,
                        decision_date=current.trade_date,
                        next_trade_date=following.trade_date,
                        next_open=following.open,
                        next_close=following.close,
                    )
                )
        return sort_records(labels)


V21BootstrapProvider = V2BootstrapProvider


__all__ = [
    "DEFAULT_V2_DATABASE",
    "V2BootstrapProvider",
    "V21BootstrapProvider",
]
