"""Tushare daily + adjustment-factor market provider for Kinace V3.2.

The provider keeps Tushare ``daily`` prices in their raw, executable basis and
stores ``adj_factor`` rows separately. Research code can therefore reconstruct
an adjusted series without allowing a later endpoint factor to rewrite raw
historical prices.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Sequence

from ..models import (
    AdjustmentFactorRecord,
    DailyBarRecord,
    DailyBasicMetricRecord,
    ForwardLabelRecord,
    SecurityStateRecord,
    StockMetadataRecord,
    TradeCalendarRecord,
)
from .base import ProviderDataError, sort_records
from .tencent_provider import MarketBundle
from .tushare_provider import TushareProProvider


def _bare_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return code.split(".", 1)[0] if "." in code else code


def _tushare_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if "." in code:
        symbol, exchange = code.split(".", 1)
        if exchange in {"SH", "SZ", "BJ"}:
            return f"{symbol}.{exchange}"
        raise ProviderDataError(f"unsupported Tushare exchange suffix: {value!r}")
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _number(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderDataError(
            f"Tushare returned a non-numeric {field}: {value!r}"
        ) from exc


def _round_price(value: float) -> float:
    return int(value * 100 + 0.5) / 100.0


def _limit_pct(code: str) -> float:
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


class TushareMarketProvider:
    """Build V3 market datasets from Tushare raw daily bars and adj factors."""

    source_name = "tushare_daily_adj"
    price_basis = os.environ.get("KINACE_TUSHARE_PRICE_BASIS", "raw").strip().lower()
    batch_min_codes = 8
    batch_max_days = 31

    def __init__(
        self,
        *,
        provider: Any | None = None,
        token: str | None = None,
        endpoint: str = "https://api.tushare.pro",
        timeout: float = 30.0,
        max_retries: int = 4,
        retry_backoff: float = 1.0,
        default_count: int = 520,
    ) -> None:
        self.provider = provider or TushareProProvider(
            token=token,
            endpoint=endpoint,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        self.default_count = int(default_count)
        if self.default_count <= 0:
            raise ProviderDataError("default_count must be positive")

    @staticmethod
    def _resolve_dates(
        start_date: str | None,
        end_date: str | None,
        count: int,
    ) -> tuple[str, str]:
        resolved_end = end_date or date.today().isoformat()
        resolved_start = start_date or (
            date.fromisoformat(resolved_end) - timedelta(days=max(count * 2, 365))
        ).isoformat()
        if resolved_start > resolved_end:
            raise ProviderDataError("start_date must not be later than end_date")
        return resolved_start, resolved_end

    def _fetch_code(
        self,
        ts_code: str,
        *,
        start_date: str,
        end_date: str,
    ) -> tuple[list[DailyBarRecord], dict[str, AdjustmentFactorRecord]]:
        api_code = _tushare_code(ts_code)
        bars = list(
            self.provider.fetch_daily_bars(
                ts_code=api_code,
                start_date=start_date,
                end_date=end_date,
                fields=(
                    "ts_code,trade_date,open,high,low,close,pre_close,"
                    "change,pct_chg,vol,amount"
                ),
            )
        )
        factors = list(
            self.provider.fetch_adjustment_factors(
                ts_code=api_code,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,adj_factor",
            )
        )
        factor_by_date = {
            str(record.trade_date): record
            for record in factors
            if getattr(record, "trade_date", None) is not None
        }
        if not bars:
            return [], factor_by_date
        missing = sorted(
            {
                str(bar.trade_date)
                for bar in bars
                if str(bar.trade_date) not in factor_by_date
            }
        )
        if missing:
            preview = ", ".join(missing[:5])
            raise ProviderDataError(
                f"Tushare adj_factor is missing for {api_code}: {preview}"
            )
        return bars, factor_by_date

    def _fetch_date_window(
        self,
        requested_codes: Sequence[str],
        *,
        start_date: str,
        end_date: str,
    ) -> dict[str, tuple[list[DailyBarRecord], dict[str, AdjustmentFactorRecord]]]:
        requested = {_bare_code(code) for code in requested_codes}
        bars_by_code: dict[str, list[DailyBarRecord]] = {
            code: [] for code in requested
        }
        factors_by_code: dict[str, dict[str, AdjustmentFactorRecord]] = {
            code: {} for code in requested
        }

        calendar = self.provider.trade_cal(
            exchange="SSE",
            start_date=start_date,
            end_date=end_date,
            fields="exchange,cal_date,is_open,pretrade_date",
        )
        trade_dates = [
            str(record.cal_date)
            for record in calendar
            if bool(record.is_open)
            and start_date <= str(record.cal_date) <= end_date
        ]
        if not trade_dates:
            current = date.fromisoformat(start_date)
            final = date.fromisoformat(end_date)
            while current <= final:
                trade_dates.append(current.isoformat())
                current += timedelta(days=1)

        for trade_date in sorted(set(trade_dates)):
            for bar in self.provider.daily(
                trade_date=trade_date,
                fields=(
                    "ts_code,trade_date,open,high,low,close,pre_close,"
                    "change,pct_chg,vol,amount"
                ),
            ):
                code = _bare_code(getattr(bar, "ts_code", ""))
                if code in requested:
                    bars_by_code[code].append(bar)
            for factor in self.provider.adj_factor(
                trade_date=trade_date,
                fields="ts_code,trade_date,adj_factor",
            ):
                code = _bare_code(getattr(factor, "ts_code", ""))
                if code not in requested:
                    continue
                trade_day = getattr(factor, "trade_date", None)
                if trade_day is not None:
                    factors_by_code[code][str(trade_day)] = factor

        return {
            code: (bars_by_code[code], factors_by_code[code])
            for code in requested
        }

    def _use_date_window(self, codes: Sequence[str], start_date: str, end_date: str) -> bool:
        if len(codes) < self.batch_min_codes:
            return False
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        return (end - start).days <= self.batch_max_days

    def fetch_history_bundle(
        self,
        ts_codes: Sequence[str],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        count: int | None = None,
    ) -> MarketBundle:
        resolved_start, resolved_end = self._resolve_dates(
            start_date, end_date, int(count or self.default_count)
        )
        daily_bars: list[DailyBarRecord] = []
        daily_basic: list[DailyBasicMetricRecord] = []
        states: list[SecurityStateRecord] = []
        labels: list[ForwardLabelRecord] = []
        adjustment_factors: list[AdjustmentFactorRecord] = []
        calendar_dates: set[str] = set()

        requested_codes = sorted(
            {_bare_code(code) for code in ts_codes if str(code).strip()}
        )
        window = (
            self._fetch_date_window(
                requested_codes,
                start_date=resolved_start,
                end_date=resolved_end,
            )
            if self._use_date_window(requested_codes, resolved_start, resolved_end)
            else None
        )
        for requested_code in requested_codes:
            if window is None:
                bars, factors = self._fetch_code(
                    requested_code,
                    start_date=resolved_start,
                    end_date=resolved_end,
                )
            else:
                bars, factors = window[requested_code]
            if not bars:
                continue
            ordered = sorted(bars, key=lambda item: item.trade_date)
            missing = sorted(
                {
                    str(bar.trade_date)
                    for bar in ordered
                    if str(bar.trade_date) not in factors
                }
            )
            if missing:
                preview = ", ".join(missing[:5])
                raise ProviderDataError(
                    f"Tushare adj_factor is missing for {requested_code}: {preview}"
                )
            basis_date = max(str(record.trade_date) for record in factors.values())
            for trade_date, factor in factors.items():
                factor_value = _number(factor.adj_factor, "adj_factor")
                if factor_value <= 0:
                    raise ProviderDataError(
                        f"Tushare adj_factor must be positive for {requested_code} "
                        f"on {trade_date}"
                    )
                adjustment_factors.append(
                    AdjustmentFactorRecord(
                        ts_code=requested_code,
                        trade_date=trade_date,
                        adj_factor=factor_value,
                        adjustment_basis_date=basis_date,
                    )
                )

            previous_raw_close: float | None = None
            volumes: list[float] = []
            raw_rows: list[dict[str, float | str]] = []
            for bar in ordered:
                trade_date = str(bar.trade_date)
                raw_close = _number(bar.close, "close")
                open_price = _number(bar.open, "open")
                high = _number(bar.high, "high")
                low = _number(bar.low, "low")
                volume = (
                    None if bar.vol is None else _number(bar.vol, "vol") * 100.0
                )
                amount = (
                    None
                    if bar.amount is None
                    else _number(bar.amount, "amount") * 1000.0
                )
                raw_pre_close = (
                    _number(bar.pre_close, "pre_close")
                    if bar.pre_close is not None
                    else previous_raw_close
                )
                change = (
                    raw_close - raw_pre_close
                    if raw_pre_close is not None
                    else None
                )
                pct_chg = (
                    raw_close / raw_pre_close - 1.0 if raw_pre_close else None
                )
                daily_bars.append(
                    DailyBarRecord(
                        ts_code=requested_code,
                        trade_date=trade_date,
                        open=open_price,
                        high=high,
                        low=low,
                        close=raw_close,
                        pre_close=raw_pre_close,
                        change=change,
                        pct_chg=pct_chg,
                        vol=volume,
                        amount=amount,
                    )
                )
                volumes.append(volume or 0.0)
                average_volume = sum(volumes[-5:]) / min(len(volumes), 5)
                daily_basic.append(
                    DailyBasicMetricRecord(
                        ts_code=requested_code,
                        trade_date=trade_date,
                        close=raw_close,
                        volume_ratio=(
                            volume / average_volume if average_volume else None
                        ),
                    )
                )
                limit = _limit_pct(requested_code)
                up_limit = (
                    _round_price(raw_pre_close * (1 + limit))
                    if raw_pre_close
                    else None
                )
                down_limit = (
                    _round_price(raw_pre_close * (1 - limit))
                    if raw_pre_close
                    else None
                )
                suspended = bool((volume or 0.0) <= 0.0)
                states.append(
                    SecurityStateRecord(
                        ts_code=requested_code,
                        trade_date=trade_date,
                        is_suspended=suspended,
                        is_limit_up=bool(
                            not suspended
                            and raw_pre_close is not None
                            and raw_close
                            >= _round_price(raw_pre_close * (1 + limit)) - 0.001
                        ),
                        is_limit_down=bool(
                            not suspended
                            and raw_pre_close is not None
                            and raw_close
                            <= _round_price(raw_pre_close * (1 - limit)) + 0.001
                        ),
                        prev_close=raw_pre_close,
                        up_limit=up_limit,
                        down_limit=down_limit,
                    )
                )
                raw_rows.append(
                    {
                        "trade_date": trade_date,
                        "open": open_price,
                        "close": raw_close,
                    }
                )
                previous_raw_close = raw_close
                calendar_dates.add(trade_date)

            for current, following in zip(raw_rows, raw_rows[1:]):
                labels.append(
                    ForwardLabelRecord(
                        ts_code=requested_code,
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
        bundle = MarketBundle(
            daily_bars=tuple(sort_records(daily_bars)),
            trade_calendar=tuple(sort_records(calendar)),
            security_state=tuple(sort_records(states)),
            forward_labels=tuple(sort_records(labels)),
            daily_basic_metrics=tuple(sort_records(daily_basic)),
            adjustment_factors=tuple(sort_records(adjustment_factors)),
            stock_metadata=tuple(
                sort_records(
                    [
                        StockMetadataRecord(ts_code=code, name=code)
                        for code in {_bare_code(item) for item in ts_codes}
                    ]
                )
            ),
        )
        if self.price_basis != "raw":
            bundle = _legacy_adjusted_bundle(bundle)
        return bundle


def _legacy_adjusted_bundle(bundle: MarketBundle) -> MarketBundle:
    """Rebuild legacy research-price series from raw bars and adj factors."""

    factors_by_code: dict[str, dict[str, float]] = {}
    for factor in bundle.adjustment_factors:
        factors_by_code.setdefault(factor.ts_code, {})[factor.trade_date] = float(
            factor.adj_factor
        )
    ratios: dict[tuple[str, str], float] = {}
    for code, factors in factors_by_code.items():
        latest_date = max(factors)
        latest_factor = factors[latest_date]
        if latest_factor <= 0:
            raise ProviderDataError(f"Tushare adj_factor must be positive for {code}")
        for trade_date, factor_value in factors.items():
            ratios[(code, trade_date)] = factor_value / latest_factor

    bars_by_code: dict[str, list[DailyBarRecord]] = {}
    for record in bundle.daily_bars:
        bars_by_code.setdefault(record.ts_code, []).append(record)
    adjusted_bars: list[DailyBarRecord] = []
    for code, records in bars_by_code.items():
        previous_close: float | None = None
        for record in sorted(records, key=lambda item: item.trade_date):
            ratio = ratios.get((code, record.trade_date))
            if ratio is None:
                raise ProviderDataError(
                    f"Tushare adj_factor is missing for {code} on {record.trade_date}"
                )
            close = record.close * ratio
            change = close - previous_close if previous_close is not None else None
            pct_chg = close / previous_close - 1.0 if previous_close else None
            adjusted_bars.append(
                replace(
                    record,
                    open=record.open * ratio,
                    high=record.high * ratio,
                    low=record.low * ratio,
                    close=close,
                    pre_close=previous_close,
                    change=change,
                    pct_chg=pct_chg,
                )
            )
            previous_close = close

    adjusted_basics = [
        replace(
            record,
            close=record.close * ratios[(record.ts_code, record.trade_date)],
        )
        for record in bundle.daily_basic_metrics
    ]
    adjusted_states = []
    for record in bundle.security_state:
        ratio = ratios[(record.ts_code, record.trade_date)]
        adjusted_states.append(
            replace(
                record,
                prev_close=(
                    record.prev_close * ratio
                    if record.prev_close is not None
                    else None
                ),
                up_limit=(
                    record.up_limit * ratio if record.up_limit is not None else None
                ),
                down_limit=(
                    record.down_limit * ratio
                    if record.down_limit is not None
                    else None
                ),
            )
        )
    adjusted_labels = [
        replace(
            record,
            next_open=record.next_open
            * ratios[(record.ts_code, record.next_trade_date)],
            next_close=record.next_close
            * ratios[(record.ts_code, record.next_trade_date)],
        )
        for record in bundle.forward_labels
    ]
    return MarketBundle(
        daily_bars=tuple(adjusted_bars),
        trade_calendar=bundle.trade_calendar,
        security_state=tuple(adjusted_states),
        forward_labels=tuple(adjusted_labels),
        daily_basic_metrics=tuple(adjusted_basics),
        adjustment_factors=tuple(),
        stock_metadata=bundle.stock_metadata,
    )


__all__ = ["TushareMarketProvider"]
