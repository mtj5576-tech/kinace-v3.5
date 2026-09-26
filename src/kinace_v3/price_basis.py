"""Explicit price-series contracts for execution and research consumers."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Mapping, Sequence


RAW_PRICE_BASIS = "raw"
ADJUSTED_PRICE_BASIS = "adjusted"
TOTAL_RETURN_PRICE_BASIS = "total_return"
EXECUTION_PRICE_BASIS = RAW_PRICE_BASIS
RESEARCH_PRICE_BASIS = TOTAL_RETURN_PRICE_BASIS
RESEARCH_PRICE_BASES = frozenset({ADJUSTED_PRICE_BASIS, TOTAL_RETURN_PRICE_BASIS})
SUPPORTED_PRICE_BASES = frozenset(
    {RAW_PRICE_BASIS, ADJUSTED_PRICE_BASIS, TOTAL_RETURN_PRICE_BASIS}
)


class PriceBasisError(ValueError):
    """Raised when raw and research price series cannot be separated safely."""


def normalize_price_basis(value: Any, *, field: str = "price_basis") -> str:
    if not isinstance(value, str) or not value.strip():
        raise PriceBasisError(f"{field} must be a non-empty string")
    normalized = value.strip().lower()
    if normalized == "provider_adjusted":
        normalized = ADJUSTED_PRICE_BASIS
    if normalized not in SUPPORTED_PRICE_BASES:
        raise PriceBasisError(f"{field} must be raw, adjusted, or total_return")
    return normalized


def tag_price_series(bars: Mapping[str, Any], price_basis: str) -> dict[str, Any]:
    """Return one unambiguous price-series envelope."""
    resolved_basis = normalize_price_basis(price_basis)
    if not isinstance(bars, Mapping):
        raise PriceBasisError("bars must be an object")
    return {"price_basis": resolved_basis, "bars": bars}


def unpack_price_series(
    value: Any,
    *,
    expected_basis: str | None = None,
    default_basis: str | None = None,
    field: str = "bars",
) -> tuple[Any, str]:
    """Validate and unwrap a price-series envelope without guessing its basis."""
    if not isinstance(value, Mapping):
        if default_basis is None:
            raise PriceBasisError(f"{field}.price_basis must be explicit")
        return value, normalize_price_basis(default_basis, field=f"{field}.price_basis")

    expected = (
        normalize_price_basis(expected_basis, field=f"{field}.expected_price_basis")
        if expected_basis is not None
        else None
    )
    basis_keys = sorted(key for key in SUPPORTED_PRICE_BASES if key in value)
    if len(basis_keys) > 1:
        raise PriceBasisError(
            f"{field} mixes multiple price bases: " + ", ".join(basis_keys)
        )

    if "bars" in value:
        extra_series = sorted(set(basis_keys) - {str(value.get("price_basis") or "")})
        if extra_series:
            raise PriceBasisError(
                f"{field} mixes tagged bars with another price basis: "
                + ", ".join(extra_series)
            )
        if "price_basis" not in value:
            if expected is not None:
                return value["bars"], expected
            if default_basis is not None:
                return value["bars"], normalize_price_basis(
                    default_basis, field=f"{field}.price_basis"
                )
            raise PriceBasisError(f"{field}.price_basis must be explicit")
        stated = normalize_price_basis(
            value.get("price_basis"), field=f"{field}.price_basis"
        )
        if expected is not None and stated != expected:
            raise PriceBasisError(
                f"{field} price_basis={stated!r} does not match expected {expected!r}"
            )
        return value["bars"], stated

    if len(basis_keys) == 1:
        stated = str(basis_keys[0])
        if expected is not None and stated != expected:
            raise PriceBasisError(
                f"{field} price_basis={stated!r} does not match expected {expected!r}"
            )
        return value[stated], stated

    if expected is not None:
        return value, expected
    if default_basis is not None:
        return value, normalize_price_basis(default_basis, field=f"{field}.price_basis")
    raise PriceBasisError(f"{field}.price_basis must be explicit")


def unpack_market_views(value: Any) -> tuple[Any, str | None, Any | None, str | None] | None:
    """Recognize a combined execution/research market payload."""
    if not isinstance(value, Mapping):
        return None
    aliases = {
        "execution_bars": "execution",
        "execution": "execution",
        "raw_bars": "execution",
        "raw": "execution",
        "research_bars": "research",
        "research": "research",
        "adjusted_bars": "research",
        "adjusted": "research",
        "total_return_bars": "research",
        "total_return": "research",
    }
    selected = [key for key in aliases if key in value]
    if not selected:
        return None
    allowed = set(selected) | {"price_basis", "research_price_basis", "bars"}
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise PriceBasisError(
            "combined market input mixes series fields: " + ", ".join(unexpected)
        )
    execution_keys = [key for key in selected if aliases[key] == "execution"]
    research_keys = [key for key in selected if aliases[key] == "research"]
    if len(execution_keys) > 1 or len(research_keys) > 1:
        raise PriceBasisError("combined market input repeats an execution or research series")
    execution = value[execution_keys[0]] if execution_keys else value.get("bars")
    research = value[research_keys[0]] if research_keys else value.get("research_bars")
    if execution is None:
        execution = value.get("bars")
    if execution is None:
        raise PriceBasisError("combined market input requires an execution series")
    execution_basis = str(value.get("price_basis") or "").strip() or None
    research_basis = str(value.get("research_price_basis") or "").strip() or None
    if execution_basis is None and execution_keys and execution_keys[0] in SUPPORTED_PRICE_BASES:
        execution_basis = str(execution_keys[0])
    if research_basis is None and research_keys and research_keys[0] in SUPPORTED_PRICE_BASES:
        research_basis = str(research_keys[0])
    return execution, execution_basis, research, research_basis


def _bare_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0] if "." in text else text


def _record_value(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        nested = record.get("record")
        if isinstance(nested, Mapping):
            return nested.get(name)
        return record.get(name)
    return getattr(record, name, None)


def _factor_map(adjustment_factors: Any) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = defaultdict(dict)
    if adjustment_factors is None:
        return result

    def add(code: Any, trade_date: Any, factor: Any) -> None:
        resolved_code = _bare_code(code)
        resolved_date = str(trade_date or "").strip()
        if not resolved_code or not resolved_date:
            raise PriceBasisError("adjustment factor code and trade_date are required")
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            raise PriceBasisError("adjustment factor must be a finite positive number")
        number = float(factor)
        if not math.isfinite(number) or number <= 0:
            raise PriceBasisError("adjustment factor must be a finite positive number")
        existing = result[resolved_code].get(resolved_date)
        if existing is not None and existing != number:
            raise PriceBasisError(
                f"conflicting adjustment factors for {resolved_code}/{resolved_date}"
            )
        result[resolved_code][resolved_date] = number

    if isinstance(adjustment_factors, Mapping):
        for code, value in adjustment_factors.items():
            if isinstance(value, Mapping):
                for trade_date, factor in value.items():
                    add(code, trade_date, factor)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for record in value:
                    add(
                        code,
                        _record_value(record, "trade_date"),
                        _record_value(record, "adj_factor"),
                    )
            else:
                raise PriceBasisError("adjustment factors must be grouped by code and date")
    elif isinstance(adjustment_factors, Sequence) and not isinstance(
        adjustment_factors, (str, bytes)
    ):
        for record in adjustment_factors:
            add(
                _record_value(record, "ts_code"),
                _record_value(record, "trade_date"),
                _record_value(record, "adj_factor"),
            )
    else:
        raise PriceBasisError("adjustment factors must be a mapping or sequence")
    return result


def derive_adjusted_bars(
    raw_bars: Mapping[str, Sequence[Mapping[str, Any]]],
    adjustment_factors: Any = (),
    *,
    output_basis: str = ADJUSTED_PRICE_BASIS,
) -> dict[str, Any]:
    """Derive a continuous research series while leaving execution bars untouched."""
    resolved_basis = normalize_price_basis(output_basis)
    if resolved_basis not in RESEARCH_PRICE_BASES:
        raise PriceBasisError("derived research bars require adjusted or total_return basis")
    factors = _factor_map(adjustment_factors)
    adjusted: dict[str, list[dict[str, Any]]] = {}
    for supplied_code, rows in raw_bars.items():
        code = str(supplied_code)
        factor_code = _bare_code(supplied_code) or code
        code_factors = factors.get(factor_code, {})
        if not code_factors:
            base_factor = 1.0
        else:
            factor_dates = sorted(code_factors)
            price_dates = [
                str(row.get("trade_date") or "")
                for row in rows
                if str(row.get("trade_date") or "")
            ]
            base_date = min(factor_dates + price_dates)
            base_factor = code_factors.get(base_date, code_factors[factor_dates[0]])
        output_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise PriceBasisError("raw bar rows must be objects")
            trade_date = str(row.get("trade_date") or "")
            if code_factors and trade_date not in code_factors:
                raise PriceBasisError(
                    f"adjustment factor is missing for {code}/{trade_date}"
                )
            scale = (
                float(code_factors[trade_date]) / float(base_factor)
                if code_factors
                else 1.0
            )
            adjusted_row = dict(row)
            for field in ("open", "high", "low", "close", "pre_close"):
                value = row.get(field)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise PriceBasisError(f"{field} must be numeric for {code}/{trade_date}")
                adjusted_row[field] = float(value) * scale
            pre_close = adjusted_row.get("pre_close")
            close = adjusted_row.get("close")
            if pre_close not in (None, 0) and close is not None:
                adjusted_row["change"] = float(close) - float(pre_close)
                adjusted_row["pct_chg"] = float(close) / float(pre_close) - 1.0
            volume = row.get("vol", row.get("volume"))
            if volume is not None:
                if isinstance(volume, bool) or not isinstance(volume, (int, float)):
                    raise PriceBasisError(f"volume must be numeric for {code}/{trade_date}")
                adjusted_volume = float(volume) / scale
                adjusted_row["vol" if "vol" in row else "volume"] = adjusted_volume
            adjusted_row["price_basis"] = resolved_basis
            output_rows.append(adjusted_row)
        adjusted[code] = output_rows
    return tag_price_series(adjusted, resolved_basis)


__all__ = [
    "ADJUSTED_PRICE_BASIS",
    "EXECUTION_PRICE_BASIS",
    "RAW_PRICE_BASIS",
    "RESEARCH_PRICE_BASES",
    "RESEARCH_PRICE_BASIS",
    "SUPPORTED_PRICE_BASES",
    "TOTAL_RETURN_PRICE_BASIS",
    "PriceBasisError",
    "derive_adjusted_bars",
    "normalize_price_basis",
    "tag_price_series",
    "unpack_market_views",
    "unpack_price_series",
]
