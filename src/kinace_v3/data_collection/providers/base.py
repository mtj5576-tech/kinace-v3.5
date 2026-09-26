from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..models import (
    RECORD_MODELS,
    BaseRecord,
    DataCollectionModelError,
    canonical_json_bytes,
    normalize_dataset,
)


class ProviderError(RuntimeError):
    """Base error for provider configuration, transport, and data failures."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider is not configured correctly."""


class ProviderDataError(ProviderError):
    """Raised when source data cannot be normalized into canonical records."""


class ProviderTransportError(ProviderError):
    """Raised when a transport fails before returning usable provider data."""


class ProviderAPIError(ProviderError):
    """Raised when a remote provider returns an application-level error."""


class ProviderRateLimitError(ProviderAPIError):
    """Raised when a provider rate limit persists after configured retries."""


TransportError = ProviderTransportError
APIError = ProviderAPIError
RateLimitError = ProviderRateLimitError
ConfigurationError = ProviderConfigurationError
DataError = ProviderDataError
TushareAPIError = ProviderAPIError
TushareRateLimitError = ProviderRateLimitError
TushareTransportError = ProviderTransportError
TushareConfigurationError = ProviderConfigurationError
TushareDataError = ProviderDataError


@runtime_checkable
class Provider(Protocol):
    """Source-independent contract consumed by data ingestion."""

    source_name: str

    def fetch_trade_calendar(self, *args: Any, **kwargs: Any) -> Sequence[BaseRecord]:
        """Return normalized trade-calendar records."""

    def fetch_stock_metadata(self, *args: Any, **kwargs: Any) -> Sequence[BaseRecord]:
        """Return normalized stock metadata records."""

    def fetch_daily_bars(self, *args: Any, **kwargs: Any) -> Sequence[BaseRecord]:
        """Return normalized daily-bar records."""

    def fetch_daily_basic_metrics(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[BaseRecord]:
        """Return normalized daily-basic metric records."""

    def fetch_adjustment_factors(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[BaseRecord]:
        """Return normalized adjustment-factor records."""

    def fetch_name_change_intervals(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[BaseRecord]:
        """Return normalized name-change interval records."""

    def fetch_sector_membership(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[BaseRecord]:
        """Return normalized sector-membership records."""

    def fetch_security_state(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[BaseRecord]:
        """Return normalized suspended and limit-state records."""

    def fetch_forward_labels(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[BaseRecord]:
        """Return normalized next-session forward labels."""


DataProvider = Provider


REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "trade_calendar": frozenset({"exchange", "cal_date", "is_open"}),
    "stock_metadata": frozenset({"ts_code", "name"}),
    "daily_bars": frozenset(
        {"ts_code", "trade_date", "open", "high", "low", "close"}
    ),
    "daily_basic_metrics": frozenset({"ts_code", "trade_date"}),
    "adjustment_factors": frozenset({"ts_code", "trade_date", "adj_factor"}),
    "name_change_intervals": frozenset({"ts_code", "name", "start_date"}),
    "sector_membership": frozenset({"index_code", "con_code", "in_date"}),
    "security_state": frozenset(
        {"ts_code", "trade_date", "is_suspended", "is_limit_up", "is_limit_down"}
    ),
    "forward_labels": frozenset(
        {"ts_code", "decision_date", "next_trade_date", "next_open", "next_close"}
    ),
}

DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "trade_calendar": ("cal_date", "pretrade_date"),
    "stock_metadata": ("list_date", "delist_date"),
    "daily_bars": ("trade_date",),
    "daily_basic_metrics": ("trade_date",),
    "adjustment_factors": ("trade_date", "adjustment_basis_date"),
    "name_change_intervals": ("start_date", "end_date", "ann_date"),
    "sector_membership": ("in_date", "out_date"),
    "security_state": ("trade_date",),
    "forward_labels": ("decision_date", "next_trade_date"),
}

OPTIONAL_DATE_FIELDS = frozenset(
    {
        "pretrade_date",
        "list_date",
        "delist_date",
        "end_date",
        "ann_date",
        "out_date",
        "adjustment_basis_date",
    }
)

NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    "daily_bars": (
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    ),
    "daily_basic_metrics": (
        "close",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
        "dv_ratio",
        "dv_ttm",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
    "adjustment_factors": ("adj_factor",),
    "security_state": ("prev_close", "up_limit", "down_limit"),
    "forward_labels": ("next_open", "next_close", "forward_return"),
}

TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "trade_calendar": ("exchange",),
    "stock_metadata": (
        "ts_code",
        "name",
        "symbol",
        "area",
        "industry",
        "market",
        "exchange",
        "list_status",
        "is_hs",
    ),
    "daily_bars": ("ts_code",),
    "daily_basic_metrics": ("ts_code",),
    "adjustment_factors": ("ts_code",),
    "name_change_intervals": ("ts_code", "name", "change_reason"),
    "sector_membership": ("index_code", "con_code", "is_new"),
    "security_state": ("ts_code",),
    "forward_labels": ("ts_code",),
}

MISSING_TEXT = frozenset({"", "none", "null", "nan", "nat"})


def _missing(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().lower() in MISSING_TEXT
    )


def normalize_date_value(
    value: Any,
    field: str,
    *,
    optional: bool = False,
) -> str | None:
    """Normalize ISO, compact, date, and datetime values to an ISO date string."""
    if _missing(value):
        if optional:
            return None
        raise ProviderDataError(f"{field} is required")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProviderDataError(f"{field} must be an ISO or YYYYMMDD date")

    text = str(value).strip()
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ProviderDataError(
            f"{field} must be an ISO date or compact YYYYMMDD date"
        ) from None


def _normalize_number(value: Any, field: str, *, optional: bool) -> float | None:
    if _missing(value):
        if optional:
            return None
        raise ProviderDataError(f"{field} is required")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ProviderDataError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ProviderDataError(f"{field} must be numeric") from None
    if not math.isfinite(number):
        raise ProviderDataError(f"{field} must be finite")
    return number


def _normalize_text(value: Any, field: str, *, optional: bool) -> str | None:
    if _missing(value):
        if optional:
            return None
        raise ProviderDataError(f"{field} is required")
    if isinstance(value, bool):
        text = "1" if value else "0"
    elif isinstance(value, (str, int, float)):
        text = str(value).strip()
    else:
        raise ProviderDataError(f"{field} must be text")
    if not text:
        if optional:
            return None
        raise ProviderDataError(f"{field} is required")
    return text


def _normalize_boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "open"}:
            return True
        if normalized in {"0", "false", "no", "n", "closed"}:
            return False
    raise ProviderDataError(f"{field} must be a boolean")


def normalize_raw_rows(
    dataset: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
    context: str = "row",
) -> list[BaseRecord]:
    """Coerce source rows into canonical records and return deterministic order."""
    resolved_dataset = normalize_dataset(dataset)
    model = RECORD_MODELS[resolved_dataset]
    required = REQUIRED_COLUMNS[resolved_dataset]
    numeric = frozenset(NUMERIC_FIELDS.get(resolved_dataset, ()))
    dates = frozenset(DATE_FIELDS.get(resolved_dataset, ()))
    text_fields = frozenset(TEXT_FIELDS.get(resolved_dataset, ()))
    records: list[BaseRecord] = []

    for index, source_row in enumerate(rows, start=1):
        if not isinstance(source_row, Mapping):
            raise ProviderDataError(
                f"{source}: {context} {index} must be an object, not "
                f"{type(source_row).__name__}"
            )
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in source_row.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ProviderDataError(
                    f"{source}: {context} {index} contains an invalid field name"
                )
            key = raw_key.strip()
            if key in normalized:
                raise ProviderDataError(
                    f"{source}: {context} {index} contains duplicate field {key}"
                )
            normalized[key] = raw_value

        missing = sorted(required.difference(normalized))
        if missing:
            raise ProviderDataError(
                f"{source}: {context} {index} is missing required fields: "
                + ", ".join(missing)
            )

        for field in dates.intersection(normalized):
            normalized[field] = normalize_date_value(
                normalized[field],
                field,
                optional=field in OPTIONAL_DATE_FIELDS,
            )
        for field in numeric.intersection(normalized):
            normalized[field] = _normalize_number(
                normalized[field],
                field,
                optional=field not in required,
            )
        for field in text_fields.intersection(normalized):
            normalized[field] = _normalize_text(
                normalized[field],
                field,
                optional=field not in required,
            )
        if "is_open" in normalized:
            normalized["is_open"] = _normalize_boolean(
                normalized["is_open"], "is_open"
            )

        try:
            record = model.from_dict(normalized)
        except DataCollectionModelError as exc:
            raise ProviderDataError(
                f"{source}: {context} {index} is invalid: {exc}"
            ) from None
        records.append(record)

    return sort_records(records)


def sort_records(records: Sequence[BaseRecord]) -> list[BaseRecord]:
    """Sort records by their canonical natural key for deterministic output."""
    return sorted(records, key=lambda record: canonical_json_bytes(record.natural_key()))


def redact_secrets(value: Any, secrets: Sequence[str]) -> str:
    """Return text with configured secrets removed."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


__all__ = [
    "APIError",
    "ConfigurationError",
    "DATE_FIELDS",
    "DataError",
    "DataProvider",
    "NUMERIC_FIELDS",
    "OPTIONAL_DATE_FIELDS",
    "Provider",
    "ProviderAPIError",
    "ProviderConfigurationError",
    "ProviderDataError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTransportError",
    "REQUIRED_COLUMNS",
    "RateLimitError",
    "TEXT_FIELDS",
    "TransportError",
    "TushareAPIError",
    "TushareConfigurationError",
    "TushareDataError",
    "TushareRateLimitError",
    "TushareTransportError",
    "normalize_date_value",
    "normalize_raw_rows",
    "redact_secrets",
    "sort_records",
]
