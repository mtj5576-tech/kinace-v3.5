"""Tencent market-data provider for Kinace V3.2.

The provider consumes Tencent's public qfq daily K-line endpoint. It returns
real market observations only: model recommendations, scores, and predictions
are never accepted as inputs.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote

from ..models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    DailyBasicMetricRecord,
    ForwardLabelRecord,
    SecurityStateRecord,
    StockMetadataRecord,
    TradeCalendarRecord,
)
from .base import ProviderDataError, ProviderTransportError, sort_records


TENCENT_HISTORY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


class TencentTransport(Protocol):
    """Small injectable transport used by provider tests."""

    def get_json(self, url: str) -> Mapping[str, Any]:
        ...


class UrllibTencentTransport:
    """Standard-library JSON transport for Tencent market observations."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = float(timeout)

    def get_json(self, url: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "kinace-v3.2-tencent-provider/1",
                "Referer": "https://gu.qq.com/",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ProviderTransportError(f"Tencent request failed: {exc}") from None
        if not isinstance(payload, Mapping):
            raise ProviderDataError("Tencent response root must be an object")
        return payload


@dataclass(frozen=True)
class MarketBundle:
    """Canonical datasets derived from one real-market fetch."""

    daily_bars: tuple[DailyBarRecord, ...]
    trade_calendar: tuple[TradeCalendarRecord, ...]
    security_state: tuple[SecurityStateRecord, ...]
    forward_labels: tuple[ForwardLabelRecord, ...]
    daily_basic_metrics: tuple[DailyBasicMetricRecord, ...]
    stock_metadata: tuple[StockMetadataRecord, ...]
    adjustment_factors: tuple[AdjustmentFactorRecord, ...] = ()


def _symbol(ts_code: str) -> str:
    code = str(ts_code).strip()
    if "." in code:
        symbol, exchange = code.split(".", 1)
        exchange = exchange.upper()
        if exchange == "SH":
            return f"sh{symbol}"
        if exchange == "SZ":
            return f"sz{symbol}"
        if exchange == "BJ":
            return f"bj{symbol}"
        code = symbol
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def _ts_code(ts_code: str) -> str:
    value = str(ts_code).strip()
    return value.split(".", 1)[0]


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderDataError(f"Tencent returned a non-numeric value: {value!r}") from exc


def _round_price(value: float) -> float:
    return int(value * 100 + 0.5) / 100.0


def _limit_pct(code: str) -> float:
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


class TencentMarketProvider:
    """Fetch genuine market bars and derive labels and trading state."""

    source_name = "tencent_market"
    price_basis = "adjusted"

    def __init__(
        self,
        *,
        transport: TencentTransport | None = None,
        timeout: float = 20.0,
        default_count: int = 520,
    ) -> None:
        self.transport = transport or UrllibTencentTransport(timeout=timeout)
        self.default_count = int(default_count)
        if self.default_count <= 0:
            raise ProviderDataError("default_count must be positive")

    def _history_url(self, ts_code: str, count: int) -> str:
        symbol = quote(_symbol(ts_code), safe="")
        return f"{TENCENT_HISTORY_URL}?param={symbol},day,,,{int(count)},qfq"

    def _history_rows(self, ts_code: str, count: int) -> list[dict[str, Any]]:
        symbol = _symbol(ts_code)
        payload = self.transport.get_json(self._history_url(ts_code, count))
        data = payload.get("data")
        node = data.get(symbol) if isinstance(data, Mapping) else None
        if not isinstance(node, Mapping):
            raise ProviderDataError(f"Tencent response is missing data for {symbol}")
        rows = node.get("qfqday") or node.get("day") or []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ProviderDataError("Tencent K-line rows must be an array")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 6:
                continue
            trade_date = str(row[0])[:10]
            open_price = _number(row[1])
            close_price = _number(row[2])
            high_price = _number(row[3])
            low_price = _number(row[4])
            # Tencent reports A-share volume in lots; the canonical Kinace
            # dataset stores shares (one lot is 100 shares).
            volume = _number(row[5]) * 100.0
            result.append(
                {
                    "ts_code": _ts_code(ts_code),
                    "trade_date": trade_date,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "vol": volume,
                    "amount": None,
                }
            )
        result.sort(key=lambda item: (item["trade_date"], item["ts_code"]))
        return result

    @staticmethod
    def _filter_rows(
        rows: Sequence[Mapping[str, Any]],
        *,
        start_date: str | None,
        end_date: str | None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            trade_date = str(row.get("trade_date") or "")
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            result.append(dict(row))
        return result

    @staticmethod
    def _codes_from_kwargs(kwargs: Mapping[str, Any]) -> list[str]:
        codes = list(kwargs.get("ts_codes") or ())
        if kwargs.get("ts_code"):
            codes.append(str(kwargs["ts_code"]))
        return sorted({_ts_code(code) for code in codes if str(code).strip()})

    def fetch_history_bundle(
        self,
        ts_codes: Sequence[str],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        count: int | None = None,
    ) -> MarketBundle:
        """Fetch bars once and derive all V3.2 market datasets from those bars."""
        resolved_count = int(count or self.default_count)
        rows_by_code: dict[str, list[dict[str, Any]]] = {}
        for ts_code in sorted({_ts_code(code) for code in ts_codes if str(code).strip()}):
            rows_by_code[ts_code] = self._filter_rows(
                self._history_rows(ts_code, resolved_count),
                start_date=start_date,
                end_date=end_date,
            )

        daily_bars: list[DailyBarRecord] = []
        daily_basic: list[DailyBasicMetricRecord] = []
        states: list[SecurityStateRecord] = []
        labels: list[ForwardLabelRecord] = []
        calendar_dates: set[str] = set()

        for code, rows in rows_by_code.items():
            if not rows:
                continue
            ordered = sorted(rows, key=lambda item: item["trade_date"])
            previous_close: float | None = None
            volumes: list[float] = []
            for index, row in enumerate(ordered):
                trade_date = str(row["trade_date"])
                close = float(row["close"])
                volume = float(row.get("vol") or 0.0)
                pre_close = previous_close
                change = close - pre_close if pre_close is not None else None
                pct_chg = close / pre_close - 1.0 if pre_close else None
                daily_bars.append(
                    DailyBarRecord(
                        ts_code=code,
                        trade_date=trade_date,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=close,
                        pre_close=pre_close,
                        change=change,
                        pct_chg=pct_chg,
                        vol=volume,
                        amount=row.get("amount"),
                    )
                )
                volumes.append(volume)
                average_volume = sum(volumes[-5:]) / min(len(volumes), 5)
                daily_basic.append(
                    DailyBasicMetricRecord(
                        ts_code=code,
                        trade_date=trade_date,
                        close=close,
                        volume_ratio=(volume / average_volume if average_volume else None),
                    )
                )
                limit = _limit_pct(code)
                up_limit = _round_price(pre_close * (1 + limit)) if pre_close else None
                down_limit = _round_price(pre_close * (1 - limit)) if pre_close else None
                suspended = volume <= 0
                states.append(
                    SecurityStateRecord(
                        ts_code=code,
                        trade_date=trade_date,
                        is_suspended=suspended,
                        is_limit_up=bool(
                            not suspended
                            and up_limit is not None
                            and close >= up_limit - 0.001
                        ),
                        is_limit_down=bool(
                            not suspended
                            and down_limit is not None
                            and close <= down_limit + 0.001
                        ),
                        prev_close=pre_close,
                        up_limit=up_limit,
                        down_limit=down_limit,
                    )
                )
                previous_close = close
                calendar_dates.add(trade_date)

            for current, following in zip(ordered, ordered[1:]):
                labels.append(
                    ForwardLabelRecord(
                        ts_code=code,
                        decision_date=str(current["trade_date"]),
                        next_trade_date=str(following["trade_date"]),
                        next_open=float(following["open"]),
                        next_close=float(following["close"]),
                    )
                )

        calendar = [
            TradeCalendarRecord(exchange="SSE", cal_date=value, is_open=True)
            for value in sorted(calendar_dates)
        ]
        return MarketBundle(
            daily_bars=tuple(sort_records(daily_bars)),
            trade_calendar=tuple(sort_records(calendar)),
            security_state=tuple(sort_records(states)),
            forward_labels=tuple(sort_records(labels)),
            daily_basic_metrics=tuple(sort_records(daily_basic)),
            stock_metadata=tuple(
                sort_records(
                    [StockMetadataRecord(ts_code=code, name=code) for code in rows_by_code]
                )
            ),
        )

    def fetch_daily_bars(
        self,
        *,
        ts_code: str | None = None,
        ts_codes: Sequence[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        count: int | None = None,
        **_: Any,
    ) -> Sequence[DailyBarRecord]:
        codes = list(ts_codes or ([ts_code] if ts_code else []))
        if not codes:
            raise ProviderDataError("ts_code or ts_codes is required")
        return self.fetch_history_bundle(
            codes, start_date=start_date, end_date=end_date, count=count
        ).daily_bars

    def fetch_trade_calendar(
        self,
        *,
        ts_codes: Sequence[str] | None = None,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        count: int | None = None,
        **_: Any,
    ) -> Sequence[TradeCalendarRecord]:
        codes = list(ts_codes or ([ts_code] if ts_code else []))
        if not codes:
            raise ProviderDataError("ts_code or ts_codes is required")
        return self.fetch_history_bundle(
            codes, start_date=start_date, end_date=end_date, count=count
        ).trade_calendar

    def fetch_security_state(self, **kwargs: Any) -> Sequence[SecurityStateRecord]:
        codes = self._codes_from_kwargs(kwargs)
        if not codes:
            raise ProviderDataError("ts_code or ts_codes is required")
        return self.fetch_history_bundle(
            codes,
            start_date=kwargs.get("start_date"),
            end_date=kwargs.get("end_date"),
            count=kwargs.get("count"),
        ).security_state

    def fetch_forward_labels(self, **kwargs: Any) -> Sequence[ForwardLabelRecord]:
        codes = self._codes_from_kwargs(kwargs)
        if not codes:
            raise ProviderDataError("ts_code or ts_codes is required")
        return self.fetch_history_bundle(
            codes,
            start_date=kwargs.get("start_date"),
            end_date=kwargs.get("end_date"),
            count=kwargs.get("count"),
        ).forward_labels

    def fetch_daily_basic_metrics(self, **kwargs: Any) -> Sequence[DailyBasicMetricRecord]:
        codes = self._codes_from_kwargs(kwargs)
        if not codes:
            raise ProviderDataError("ts_code or ts_codes is required")
        return self.fetch_history_bundle(
            codes,
            start_date=kwargs.get("start_date"),
            end_date=kwargs.get("end_date"),
            count=kwargs.get("count"),
        ).daily_basic_metrics

    def fetch_stock_metadata(self, **kwargs: Any) -> Sequence[StockMetadataRecord]:
        codes = self._codes_from_kwargs(kwargs)
        if not codes:
            return ()
        return self.fetch_history_bundle(codes, count=kwargs.get("count")).stock_metadata

    def fetch_adjustment_factors(self, **_: Any) -> Sequence[Any]:
        return ()

    def fetch_name_change_intervals(self, **_: Any) -> Sequence[Any]:
        return ()

    def fetch_sector_membership(self, **_: Any) -> Sequence[Any]:
        return ()


__all__ = [
    "MarketBundle",
    "TENCENT_HISTORY_URL",
    "TencentMarketProvider",
    "TencentTransport",
    "UrllibTencentTransport",
]
