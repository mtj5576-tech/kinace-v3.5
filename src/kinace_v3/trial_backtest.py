from __future__ import annotations

import json
import math
import sqlite3
import statistics
from bisect import bisect_right
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .experiments import initialize_experiments_database, record_backtest_evidence
from .feature_snapshot import canonical_json_bytes, sha256_bytes
from .price_basis import (
    EXECUTION_PRICE_BASIS,
    RAW_PRICE_BASIS,
    RESEARCH_PRICE_BASIS,
    RESEARCH_PRICE_BASES,
    PriceBasisError,
    derive_adjusted_bars,
    normalize_price_basis,
    tag_price_series,
    unpack_market_views,
    unpack_price_series,
)
from .score_replay import ScoreReplayError, load_replay_artifact


SCHEMA_VERSION = 1
MARK_TO_MARKET_EQUITY_BASIS = "mark_to_market"
RETURN_ANOMALY_POLICY: dict[str, Any] = {
    "policy_id": "retain_raw_returns_v1",
    "strategy": "retain_all_finite_returns",
    "benchmark": "retain_all_finite_returns",
    "sanitized_auxiliary": {
        "rule": "exclude",
        "condition": "abs(raw_return) > 0.50",
        "threshold": 0.50,
        "exclusion_reason": "absolute_return_gt_0.50",
        "role": "audit_only",
    },
}
DEFAULT_CONFIG: dict[str, Any] = {
    "initial_cash": 300_000.0,
    "max_positions": 8,
    "position_size_pct": 0.20,
    "lot_size": 100,
    "min_score": 0.0,
    "max_hold_sessions": 10,
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "slippage_bps": 5.0,
    "max_volume_participation": 0.10,
    "market_filter_enabled": False,
    "market_filter_sessions": 20,
    "min_market_return": -1.0,
    "stop_atr_multiple": 2.0,
    "target_atr_multiple": 3.0,
    "atr_period": 14,
}


class TrialBacktestError(ValueError):
    """Raised when a candidate-specific trial backtest cannot be completed."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TrialBacktestError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrialBacktestError(f"{field} must be a non-empty string")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrialBacktestError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise TrialBacktestError(f"{field} must be a finite number")
    return number


def _required_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TrialBacktestError(f"{field} must be a boolean")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TrialBacktestError(f"{field} must be a positive integer")
    return value


def _normal_date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text[:10]).isoformat()
        digits = "".join(character for character in text if character.isdigit())
        if len(digits) >= 8:
            return date.fromisoformat(
                f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
            ).isoformat()
    except ValueError as exc:
        raise TrialBacktestError(f"{field} is not a valid date: {value!r}") from exc
    raise TrialBacktestError(f"{field} is not a valid date: {value!r}")


def _iso_trade_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TrialBacktestError(f"{field} must be an ISO date string")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise TrialBacktestError(f"{field} is not an ISO date: {value!r}") from exc
    if parsed.isoformat() != text:
        raise TrialBacktestError(f"{field} is not an ISO date: {value!r}")
    return text


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(target: Path) -> sqlite3.Connection:
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def normalize_backtest_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = dict(config or {})
    unknown = sorted(set(source) - set(DEFAULT_CONFIG))
    if unknown:
        raise TrialBacktestError(
            "unknown backtest configuration keys: " + ", ".join(unknown)
        )
    result = deepcopy(DEFAULT_CONFIG)
    result.update(source)

    result["initial_cash"] = _finite_number(result["initial_cash"], "initial_cash")
    if result["initial_cash"] <= 0:
        raise TrialBacktestError("initial_cash must be positive")
    result["max_positions"] = _positive_int(result["max_positions"], "max_positions")
    result["lot_size"] = _positive_int(result["lot_size"], "lot_size")
    result["max_hold_sessions"] = _positive_int(
        result["max_hold_sessions"], "max_hold_sessions"
    )
    result["atr_period"] = _positive_int(result["atr_period"], "atr_period")
    for key in (
        "position_size_pct",
        "min_score",
        "commission_rate",
        "min_commission",
        "stamp_duty_rate",
        "transfer_fee_rate",
        "slippage_bps",
        "max_volume_participation",
        "min_market_return",
        "stop_atr_multiple",
        "target_atr_multiple",
    ):
        result[key] = _finite_number(result[key], key)
    if not 0 < result["position_size_pct"] <= 1:
        raise TrialBacktestError("position_size_pct must be in (0, 1]")
    if not 0 < result["max_volume_participation"] <= 1:
        raise TrialBacktestError("max_volume_participation must be in (0, 1]")
    if not isinstance(result["market_filter_enabled"], bool):
        raise TrialBacktestError("market_filter_enabled must be a boolean")
    result["market_filter_sessions"] = _positive_int(
        result["market_filter_sessions"], "market_filter_sessions"
    )
    if not -1 <= result["min_market_return"] <= 1:
        raise TrialBacktestError("min_market_return must be in [-1, 1]")
    if any(
        result[key] < 0
        for key in (
            "commission_rate",
            "min_commission",
            "stamp_duty_rate",
            "transfer_fee_rate",
            "slippage_bps",
            "stop_atr_multiple",
            "target_atr_multiple",
        )
    ):
        raise TrialBacktestError("fee, slippage, and ATR multipliers cannot be negative")
    return result


def trial_config_hash(candidate_id: str, config: Mapping[str, Any]) -> str:
    """Return the immutable identity of one candidate and backtest configuration."""
    return sha256_bytes(
        canonical_json_bytes(
            {
                "candidate_id": _required_text(candidate_id, "candidate_id"),
                "backtest_config": normalize_backtest_config(config),
            },
            trailing_newline=False,
        )
    )


def _json_input(value: str | Path | Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    target = Path(value)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrialBacktestError(f"cannot load market bars JSON: {exc}") from exc


def _expanded_bar_rows(value: Any) -> list[tuple[str | None, Mapping[str, Any]]]:
    if isinstance(value, (str, Path, Mapping)):
        value = _json_input(value)
    if isinstance(value, Mapping):
        source = _mapping(value, "bars")
        if "bars" in source:
            return _expanded_bar_rows(source["bars"])
        if "code" in source and ("trade_date" in source or "date" in source):
            return [(str(source["code"]), source)]
        rows: list[tuple[str | None, Mapping[str, Any]]] = []
        for code, code_rows in source.items():
            if not isinstance(code_rows, Sequence) or isinstance(code_rows, (str, bytes)):
                raise TrialBacktestError(f"bars.{code} must be a list")
            rows.extend((str(code), _mapping(row, f"bars.{code}[]")) for row in code_rows)
        return rows
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [(None, _mapping(row, "bars[]")) for row in value]
    raise TrialBacktestError("bars must be a JSON file, object, or list")


def normalize_market_bars(
    bars: Any,
    *,
    price_basis: str | None = None,
    require_execution_flags: bool = True,
    require_volume: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    source = _json_input(bars) if isinstance(bars, (str, Path)) else bars
    expected_basis = (
        normalize_price_basis(price_basis) if price_basis is not None else None
    )
    payload, resolved_basis = unpack_price_series(
        source,
        expected_basis=expected_basis,
        default_basis=expected_basis or EXECUTION_PRICE_BASIS,
    )
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for supplied_code, raw_row in _expanded_bar_rows(payload):
        row = _mapping(raw_row, "bar")
        row_basis = row.get("price_basis")
        if row_basis is not None and normalize_price_basis(
            row_basis, field="bar.price_basis"
        ) != resolved_basis:
            raise TrialBacktestError(
                "market bars mix raw, adjusted, and total-return price bases"
            )
        code = _required_text(row.get("code", supplied_code), "bar.code")
        trade_date = _iso_trade_date(
            row.get("trade_date", row.get("date")), "bar.trade_date"
        )
        parsed = {"code": code, "trade_date": trade_date}
        for field in ("open", "high", "low", "close"):
            number = _finite_number(row.get(field), f"bar.{field}")
            if number <= 0:
                raise TrialBacktestError(f"bar.{field} must be positive")
            parsed[field] = number
        volume_value = row.get("volume")
        if volume_value is None and not require_volume:
            volume = 0.0
        else:
            volume = _finite_number(volume_value, "bar.volume")
        if volume < 0:
            raise TrialBacktestError("bar.volume cannot be negative")
        parsed["volume"] = volume
        if parsed["high"] < max(parsed["open"], parsed["close"], parsed["low"]):
            raise TrialBacktestError(f"bar high is inconsistent for {code}/{trade_date}")
        if parsed["low"] > min(parsed["open"], parsed["close"], parsed["high"]):
            raise TrialBacktestError(f"bar low is inconsistent for {code}/{trade_date}")
        for field in ("is_suspended", "is_limit_up", "is_limit_down"):
            value = row.get(field)
            parsed[field] = (
                _required_bool(value, f"bar.{field}")
                if require_execution_flags
                else bool(value)
            )
        by_date = grouped.setdefault(code, {})
        if trade_date in by_date:
            raise TrialBacktestError(f"duplicate market bar: {code}/{trade_date}")
        by_date[trade_date] = parsed

    result = {
        code: sorted(by_date.values(), key=lambda item: item["trade_date"])
        for code, by_date in sorted(grouped.items())
    }
    if not result:
        raise TrialBacktestError("at least one market bar is required")
    return result


def normalize_market_views(
    bars: Any,
    research_bars: Any | None = None,
    *,
    price_basis: str | None = None,
    research_price_basis: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Normalize raw execution bars and a distinct adjusted research series."""
    try:
        combined = unpack_market_views(bars)
        explicit_execution_basis = (
            normalize_price_basis(price_basis) if price_basis is not None else None
        )
        explicit_research_basis = (
            normalize_price_basis(research_price_basis)
            if research_price_basis is not None
            else None
        )

        if combined is not None:
            if research_bars is not None:
                raise PriceBasisError(
                    "combined market input and research_bars cannot be supplied together"
                )
            (
                execution_payload,
                combined_execution_basis,
                combined_research_payload,
                combined_research_basis,
            ) = combined
            combined_execution = (
                normalize_price_basis(combined_execution_basis)
                if combined_execution_basis is not None
                else None
            )
            if (
                explicit_execution_basis is not None
                and combined_execution is not None
                and explicit_execution_basis != combined_execution
            ):
                raise PriceBasisError("execution price_basis conflicts with combined input")
            execution_basis = explicit_execution_basis or combined_execution or RAW_PRICE_BASIS
            if combined_research_payload is not None:
                if research_bars is not None:
                    raise PriceBasisError("research series was supplied more than once")
                research_payload = combined_research_payload
                nested_research_basis = (
                    normalize_price_basis(combined_research_basis)
                    if combined_research_basis is not None
                    else None
                )
                if (
                    explicit_research_basis is not None
                    and nested_research_basis is not None
                    and explicit_research_basis != nested_research_basis
                ):
                    raise PriceBasisError(
                        "research price_basis conflicts with combined input"
                    )
                research_basis_hint = explicit_research_basis or nested_research_basis
            else:
                research_payload = None
                research_basis_hint = explicit_research_basis
        else:
            execution_payload = bars
            execution_basis = explicit_execution_basis or RAW_PRICE_BASIS
            research_payload = research_bars
            research_basis_hint = explicit_research_basis

        if execution_basis != RAW_PRICE_BASIS:
            raise PriceBasisError(
                "execution, tradability, limits, lot sizing, fees, and fills require "
                "price_basis=raw"
            )
        normalized_execution = normalize_market_bars(
            execution_payload,
            price_basis=execution_basis,
        )

        if research_payload is None:
            resolved_research_basis = research_basis_hint or RESEARCH_PRICE_BASIS
            if resolved_research_basis not in RESEARCH_PRICE_BASES:
                raise PriceBasisError(
                    "research bars require adjusted or total_return price_basis"
                )
            normalized_research = normalize_market_bars(
                derive_adjusted_bars(
                    normalized_execution,
                    output_basis=resolved_research_basis,
                ),
                price_basis=resolved_research_basis,
                require_execution_flags=False,
                require_volume=False,
            )
        else:
            resolved_research_basis = (
                research_basis_hint
                or _price_basis_from_series(research_payload, field="research_bars")
            )
            if resolved_research_basis not in RESEARCH_PRICE_BASES:
                raise PriceBasisError(
                    "research bars require adjusted or total_return price_basis"
                )
            normalized_research = normalize_market_bars(
                research_payload,
                price_basis=resolved_research_basis,
                require_execution_flags=False,
                require_volume=False,
            )

        for code, execution_rows in normalized_execution.items():
            research_rows = normalized_research.get(code)
            if research_rows is None:
                raise PriceBasisError(
                    f"research bars are missing execution code: {code}"
                )
            execution_dates = {str(row["trade_date"]) for row in execution_rows}
            research_dates = {str(row["trade_date"]) for row in research_rows}
            missing_dates = sorted(execution_dates - research_dates)
            if missing_dates:
                raise PriceBasisError(
                    f"research bars are missing dates for {code}: "
                    + ", ".join(missing_dates[:5])
                )
    except PriceBasisError as exc:
        raise TrialBacktestError(str(exc)) from exc

    return {
        "execution": tag_price_series(normalized_execution, RAW_PRICE_BASIS),
        "research": tag_price_series(normalized_research, resolved_research_basis),
    }


def _price_basis_from_series(value: Any, *, field: str) -> str:
    try:
        _, basis = unpack_price_series(value, field=field)
        return basis
    except PriceBasisError as exc:
        raise TrialBacktestError(str(exc)) from exc


def _atr_as_of(
    bars: Sequence[Mapping[str, Any]], decision_date: str, period: int
) -> float:
    position = bisect_right(
        bars, decision_date, key=lambda item: str(item["trade_date"])
    )
    history = bars[max(0, position - period - 1) : position]
    if len(history) < 2:
        return 0.0
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in history:
        high = float(bar["high"])
        low = float(bar["low"])
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        true_ranges.append(true_range)
        previous_close = float(bar["close"])
    window = true_ranges[-period:]
    return sum(window) / len(window) if window else 0.0


def _can_buy(bar: Mapping[str, Any]) -> bool:
    return _buy_block_reason(bar) is None


def _buy_block_reason(bar: Mapping[str, Any]) -> str | None:
    if bool(bar["is_suspended"]):
        return "suspended"
    if bool(bar["is_limit_up"]):
        return "limit_up"
    if bar["volume"] is not None and float(bar["volume"]) <= 0:
        return "zero_volume"
    return None


def _can_sell(bar: Mapping[str, Any]) -> bool:
    return _sell_block_reason(bar) is None


def _sell_block_reason(bar: Mapping[str, Any] | None) -> str | None:
    if bar is None:
        return "missing_bar"
    if bool(bar["is_suspended"]):
        return "suspended"
    if bool(bar["is_limit_down"]):
        return "limit_down"
    if bar["volume"] is not None and float(bar["volume"]) <= 0:
        return "zero_volume"
    return None


def _buy_fees(gross: float, config: Mapping[str, Any]) -> float:
    commission = max(
        float(config["min_commission"]),
        abs(gross) * float(config["commission_rate"]),
    )
    return commission + gross * float(config["transfer_fee_rate"])


def _sell_fees(gross: float, config: Mapping[str, Any]) -> float:
    return _buy_fees(gross, config) + gross * float(config["stamp_duty_rate"])


def _normalize_scores(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, list):
        raise TrialBacktestError("replay.scores must be a list")
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw_score in enumerate(raw_scores):
        row = _mapping(raw_score, f"replay.scores[{index}]")
        code = _required_text(row.get("code"), f"replay.scores[{index}].code")
        decision_date = _normal_date(
            row.get("decision_date"), f"replay.scores[{index}].decision_date"
        )
        score = _finite_number(row.get("score"), f"replay.scores[{index}].score")
        candidate = {
            "decision_date": decision_date,
            "code": code,
            "score": score,
        }
        key = (decision_date, code)
        current = deduped.get(key)
        if current is None or score > current["score"]:
            deduped[key] = candidate
    return sorted(
        deduped.values(),
        key=lambda item: (item["decision_date"], item["code"]),
    )


def _mark_to_market(
    cash: float,
    holdings: Mapping[str, Mapping[str, Any]],
    latest_close: Mapping[str, float],
) -> float:
    return cash + sum(
        float(latest_close.get(code, holding["last_price"])) * int(holding["shares"])
        for code, holding in holdings.items()
    )


def _close_position(
    *,
    holding: Mapping[str, Any],
    trade_date: str,
    intended_exit: float,
    exit_reason: str,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    slippage_rate = float(config["slippage_bps"]) / 10_000.0
    exit_price = max(0.01, intended_exit * (1.0 - slippage_rate))
    shares = int(holding["shares"])
    gross = exit_price * shares
    exit_fees = _sell_fees(gross, config)
    proceeds = gross - exit_fees
    pnl = proceeds - float(holding["entry_cost"])
    total_fees = exit_fees + float(holding["entry_fees"])
    total_slippage = (
        float(holding["entry_slippage"])
        + abs(exit_price - intended_exit) * shares
    )
    trade = {
        "code": holding["code"],
        "decision_date": holding["decision_date"],
        "entry_date": holding["entry_date"],
        "exit_date": trade_date,
        "intended_entry": holding["intended_entry"],
        "entry_price": holding["entry_price"],
        "exit_price": exit_price,
        "shares": shares,
        "stop_loss": holding["stop_loss"],
        "take_profit": holding["take_profit"],
        "pnl": pnl,
        "return_pct": pnl / float(holding["entry_cost"]),
        "fees": total_fees,
        "slippage": total_slippage,
        "exit_reason": exit_reason,
        "hold_sessions": int(holding["hold_sessions"]),
        "metadata": {"atr": holding["atr"], "score": holding["score"]},
    }
    return trade, proceeds


def _closed_trade_stats(metrics: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "trade_count",
        "win_rate",
        "profit_factor",
        "average_trade_return",
        "total_fees",
        "total_slippage",
        "holding_periods",
    )
    return {key: deepcopy(metrics[key]) for key in keys}


def _mark_to_market_point(
    *,
    trade_date: str,
    cash: float,
    holdings: Mapping[str, Mapping[str, Any]],
    latest_close: Mapping[str, float],
    realized_pnl: float,
    turnover: float,
) -> dict[str, Any]:
    cash_value = round(cash, 6)
    position_cost = round(
        sum(float(holding["entry_cost"]) for holding in holdings.values()), 6
    )
    market_value = round(
        sum(
            float(latest_close.get(code, holding["last_price"]))
            * int(holding["shares"])
            for code, holding in holdings.items()
        ),
        6,
    )
    total_assets = cash_value + market_value
    return {
        "trade_date": trade_date,
        "cash": cash_value,
        "realized_pnl": round(realized_pnl, 6),
        "open_cost": position_cost,
        "unrealized_pnl": round(market_value - position_cost, 6),
        "market_value": market_value,
        "total_assets": total_assets,
        "mark_to_market_equity": total_assets,
        "positions": len(holdings),
        "turnover": round(turnover, 6),
    }


def _metrics(
    *,
    initial_cash: float,
    equity: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    turnover: float,
) -> dict[str, Any]:
    from .research_labeling import summarize_holding_periods

    final_equity = (
        float(equity[-1]["total_assets"]) if equity else float(initial_cash)
    )
    peak = float(initial_cash)
    max_drawdown = 0.0
    for point in equity:
        total = float(point["total_assets"])
        peak = max(peak, total)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - total) / peak)
    wins = [float(trade["pnl"]) for trade in trades if float(trade["pnl"]) > 0]
    losses = [float(trade["pnl"]) for trade in trades if float(trade["pnl"]) < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_equity = (
        sum(float(point["total_assets"]) for point in equity) / len(equity)
        if equity
        else float(initial_cash)
    )
    returns = [
        float(equity[index]["total_assets"]) / float(equity[index - 1]["total_assets"]) - 1.0
        for index in range(1, len(equity))
        if float(equity[index - 1]["total_assets"]) > 0
    ]
    periods = max(1, len(returns))
    annualized_return = (
        (final_equity / float(initial_cash)) ** (252.0 / periods) - 1.0
        if len(returns) >= 2
        else None
    )
    annualized_volatility = (
        statistics.stdev(returns) * math.sqrt(252.0) if len(returns) >= 2 else None
    )
    sharpe = (
        statistics.fmean(returns) / statistics.stdev(returns) * math.sqrt(252.0)
        if len(returns) >= 2 and statistics.stdev(returns) > 0
        else None
    )
    downside = [value for value in returns if value < 0]
    sortino = (
        statistics.fmean(returns) / statistics.stdev(downside) * math.sqrt(252.0)
        if len(returns) >= 2
        and len(downside) >= 2
        and statistics.stdev(downside) > 0
        else None
    )
    peak = float(initial_cash)
    peak_index = 0
    max_drawdown_duration = 0
    for index, point in enumerate(equity):
        total = float(point["total_assets"])
        if total >= peak:
            peak = total
            peak_index = index
        else:
            max_drawdown_duration = max(max_drawdown_duration, index - peak_index)
    unavailable_fields = {
        "annualized_return": (
            "minimum_observation_sessions_not_met" if annualized_return is None else None
        ),
        "annualized_volatility": (
            "minimum_observation_sessions_not_met"
            if annualized_volatility is None
            else None
        ),
        "sharpe": "sharpe_unavailable" if sharpe is None else None,
        "sortino": "sortino_unavailable" if sortino is None else None,
    }
    unavailable_reason = next(
        (reason for reason in unavailable_fields.values() if reason is not None),
        None,
    )
    return {
        "equity_basis": MARK_TO_MARKET_EQUITY_BASIS,
        "equity_curve": "mark_to_market_equity",
        "date_range": {
            "start": str(equity[0]["trade_date"]) if equity else None,
            "end": str(equity[-1]["trade_date"]) if equity else None,
        },
        "initial_cash": round(float(initial_cash), 6),
        "final_equity": round(final_equity, 6),
        "total_return": round(final_equity / float(initial_cash) - 1.0, 6),
        "max_drawdown": round(max_drawdown, 6),
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss else None,
        "turnover": round(turnover / average_equity, 6) if average_equity else 0.0,
        "annualized_return": (
            round(annualized_return, 6) if annualized_return is not None else None
        ),
        "annualized_volatility": (
            round(annualized_volatility, 6)
            if annualized_volatility is not None
            else None
        ),
        "sharpe": round(sharpe, 6) if sharpe is not None else None,
        "sortino": round(sortino, 6) if sortino is not None else None,
        "unavailable_fields": unavailable_fields,
        "unavailable_reason": unavailable_reason,
        "max_drawdown_duration": max_drawdown_duration,
        "average_trade_return": (
            round(sum(float(trade["return_pct"]) for trade in trades) / len(trades), 6)
            if trades
            else 0.0
        ),
        "total_fees": round(sum(float(trade["fees"]) for trade in trades), 6),
        "total_slippage": round(
            sum(float(trade["slippage"]) for trade in trades), 6
        ),
        "trading_days": len(equity),
        "holding_periods": summarize_holding_periods(trades),
    }


def _benchmark_metrics(
    *,
    initial_cash: float,
    equity: Sequence[Mapping[str, Any]],
    market: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    dates = [str(point["trade_date"]) for point in equity]
    bars_by_date: dict[str, dict[str, float]] = {}
    for code, bars in market.items():
        for bar in bars:
            date = str(bar["trade_date"])
            bars_by_date.setdefault(date, {})[code] = float(bar["close"])
    returns: list[float] = []
    raw_curve: list[dict[str, Any]] = []
    sanitized_curve: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    previous: dict[str, float] | None = None
    raw_level = float(initial_cash)
    sanitized_level = float(initial_cash)
    anomaly_policy = deepcopy(RETURN_ANOMALY_POLICY)
    anomaly_rule = anomaly_policy["sanitized_auxiliary"]
    anomaly_threshold = float(anomaly_rule["threshold"])
    exclusion_reason = str(anomaly_rule["exclusion_reason"])
    for date in dates:
        current = bars_by_date.get(date, {})
        day_return = 0.0
        sanitized_day_return = 0.0
        if previous:
            common = sorted(set(previous) & set(current))
            observations = [
                (code, current[code] / previous[code] - 1.0)
                for code in common
                if previous[code] > 0
            ]
            if observations:
                day_return = statistics.fmean(value for _, value in observations)
                returns.append(day_return)
                sanitized_observations = []
                for code, raw_return in observations:
                    if abs(raw_return) > anomaly_threshold:
                        exclusions.append(
                            {
                                "date": date,
                                "code": code,
                                "raw_return": round(raw_return, 12),
                                "exclusion_reason": exclusion_reason,
                            }
                        )
                    else:
                        sanitized_observations.append(raw_return)
                if sanitized_observations:
                    sanitized_day_return = statistics.fmean(sanitized_observations)
        raw_level *= 1.0 + day_return
        sanitized_level *= 1.0 + sanitized_day_return
        raw_curve.append(
            {
                "trade_date": date,
                "benchmark_return": round(day_return, 12),
                "level": round(raw_level, 12),
            }
        )
        sanitized_curve.append(
            {
                "trade_date": date,
                "benchmark_return": round(sanitized_day_return, 12),
                "level": round(sanitized_level, 12),
            }
        )
        if current:
            previous = current
    final = raw_level
    peak = float(initial_cash)
    max_drawdown = 0.0
    for point in raw_curve:
        total = float(point["level"])
        peak = max(peak, total)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - total) / peak)
    periods = max(1, len(returns))
    annualized_return = (
        (final / float(initial_cash)) ** (252.0 / periods) - 1.0
        if len(returns) >= 2
        else None
    )
    sharpe = (
        statistics.fmean(returns) / statistics.stdev(returns) * math.sqrt(252.0)
        if len(returns) >= 2 and statistics.stdev(returns) > 0
        else None
    )
    return {
        "benchmark_raw_total_return": round(final / float(initial_cash) - 1.0, 6),
        "benchmark_total_return": round(final / float(initial_cash) - 1.0, 6),
        "benchmark_sanitized_total_return": round(
            sanitized_level / float(initial_cash) - 1.0, 6
        ),
        "benchmark_raw_curve": raw_curve,
        "benchmark_sanitized_curve": sanitized_curve,
        "benchmark_exclusions": exclusions,
        "benchmark_anomaly_policy": anomaly_policy,
        "benchmark_annualized_return": (
            round(annualized_return, 6) if annualized_return is not None else None
        ),
        "benchmark_max_drawdown": round(max_drawdown, 6),
        "benchmark_sharpe": round(sharpe, 6) if sharpe is not None else None,
    }


def _market_regime_returns(
    market: Mapping[str, Sequence[Mapping[str, Any]]],
    sessions: int,
) -> dict[str, float]:
    dates = sorted(
        {
            str(bar["trade_date"])
            for bars in market.values()
            for bar in bars
        }
    )
    closes: dict[str, dict[str, float]] = {}
    for code, bars in market.items():
        closes[code] = {
            str(bar["trade_date"]): float(bar["close"]) for bar in bars
        }

    index_level = 1.0
    levels: dict[str, float] = {}
    previous: dict[str, float] | None = None
    for trade_date in dates:
        current = {
            code: values[trade_date]
            for code, values in closes.items()
            if trade_date in values
        }
        if previous and current:
            common = sorted(set(previous) & set(current))
            returns = [
                current[code] / previous[code] - 1.0
                for code in common
                if previous[code] > 0
            ]
            if returns:
                index_level *= 1.0 + statistics.fmean(returns)
        levels[trade_date] = index_level
        if current:
            previous = current

    result: dict[str, float] = {}
    for index, trade_date in enumerate(dates):
        if index < sessions:
            continue
        base = levels[dates[index - sessions]]
        if base > 0:
            result[trade_date] = levels[trade_date] / base - 1.0
    return result


def simulate_trial(
    *,
    scores: Sequence[Mapping[str, Any]],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    research_bars: Any | None = None,
    price_basis: str | None = None,
    research_price_basis: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the deterministic execution simulation without touching persistence."""
    settings = normalize_backtest_config(config)
    normalized_scores = _normalize_scores({"scores": list(scores)})
    market_views = normalize_market_views(
        bars,
        research_bars,
        price_basis=price_basis,
        research_price_basis=research_price_basis,
    )
    market = market_views["execution"]["bars"]
    research_market = market_views["research"]["bars"]
    market_regime = (
        _market_regime_returns(
            research_market,
            int(settings["market_filter_sessions"]),
        )
        if settings["market_filter_enabled"]
        else {}
    )

    bar_maps = {
        code: {str(bar["trade_date"]): bar for bar in code_bars}
        for code, code_bars in market.items()
    }
    signal_by_entry: dict[str, list[dict[str, Any]]] = {}
    decision_dates: list[str] = []
    diagnostics = {
        "signal_count": 0,
        "below_min_score": 0,
        "missing_market_bars": 0,
        "atr_unavailable": 0,
        "no_next_session": 0,
        "entry_candidates": 0,
        "market_filter_blocked": 0,
        "max_positions_blocked": 0,
        "existing_holding_blocked": 0,
        "missing_entry_bar": 0,
        "suspended_blocked": 0,
        "limit_up_blocked": 0,
        "zero_volume_blocked": 0,
        "volume_capped": 0,
        "volume_zero_after_cap": 0,
        "cash_blocked": 0,
        "filled_entries": 0,
    }
    for signal in normalized_scores:
        diagnostics["signal_count"] += 1
        decision_dates.append(signal["decision_date"])
        if signal["score"] < float(settings["min_score"]):
            diagnostics["below_min_score"] += 1
            continue
        code_bars = market.get(signal["code"])
        if not code_bars:
            diagnostics["missing_market_bars"] += 1
            continue
        atr = _atr_as_of(
            code_bars,
            signal["decision_date"],
            int(settings["atr_period"]),
        )
        if atr <= 0:
            diagnostics["atr_unavailable"] += 1
            continue
        next_index = bisect_right(
            code_bars, signal["decision_date"], key=lambda item: str(item["trade_date"])
        )
        if next_index >= len(code_bars):
            diagnostics["no_next_session"] += 1
            continue
        entry_bar = code_bars[next_index]
        enriched = {
            **signal,
            "entry_date": str(entry_bar["trade_date"]),
            "atr": atr,
        }
        signal_by_entry.setdefault(enriched["entry_date"], []).append(enriched)
        diagnostics["entry_candidates"] += 1

    for entries in signal_by_entry.values():
        entries.sort(key=lambda item: (-float(item["score"]), item["code"]))

    if not decision_dates:
        raise TrialBacktestError("replay artifact contains no score rows")

    minimum_decision_date = min(decision_dates)
    all_dates = sorted(
        {
            str(bar["trade_date"])
            for code_bars in market.values()
            for bar in code_bars
            if str(bar["trade_date"]) >= minimum_decision_date
        }
        | set(decision_dates)
    )
    if not all_dates:
        raise TrialBacktestError("market bars do not cover the replay decision dates")

    cash = float(settings["initial_cash"])
    holdings: dict[str, dict[str, Any]] = {}
    latest_close: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    turnover = 0.0
    realized_pnl = 0.0
    slippage_rate = float(settings["slippage_bps"]) / 10_000.0

    for trade_date in all_dates:
        for code, code_map in bar_maps.items():
            if trade_date in code_map:
                latest_close[code] = float(code_map[trade_date]["close"])

        for code in list(holdings):
            holding = holdings[code]
            if trade_date <= holding["entry_date"]:
                continue
            bar = bar_maps.get(code, {}).get(trade_date)
            if bar is None or not _can_sell(bar):
                continue
            holding["hold_sessions"] += 1
            stop = float(holding["stop_loss"])
            target = float(holding["take_profit"])
            open_price = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            exit_intent: tuple[float, str] | None = None
            if open_price <= stop:
                exit_intent = (open_price, "stop_gap")
            elif open_price >= target:
                exit_intent = (open_price, "target_gap")
            elif low <= stop:
                exit_intent = (stop, "stop")
            elif high >= target:
                exit_intent = (target, "target")
            elif holding["hold_sessions"] >= int(settings["max_hold_sessions"]):
                exit_intent = (float(bar["close"]), "time_stop")
            if exit_intent is None:
                continue
            trade, proceeds = _close_position(
                holding=holding,
                trade_date=trade_date,
                intended_exit=exit_intent[0],
                exit_reason=exit_intent[1],
                config=settings,
            )
            cash += proceeds
            realized_pnl += float(trade["pnl"])
            turnover += float(trade["exit_price"]) * int(trade["shares"])
            trades.append(trade)
            del holdings[code]

        equity_now = _mark_to_market(cash, holdings, latest_close)
        entries_allowed = True
        if settings["market_filter_enabled"]:
            market_return = market_regime.get(trade_date)
            if (
                market_return is None
                or market_return < float(settings["min_market_return"])
            ):
                entries_allowed = False
        entry_signals = signal_by_entry.get(trade_date, []) if entries_allowed else []
        if not entries_allowed:
            diagnostics["market_filter_blocked"] += len(signal_by_entry.get(trade_date, []))
        for entry_index, signal in enumerate(entry_signals):
            if len(holdings) >= int(settings["max_positions"]):
                diagnostics["max_positions_blocked"] += len(entry_signals) - entry_index
                break
            code = signal["code"]
            if code in holdings:
                diagnostics["existing_holding_blocked"] += 1
                continue
            bar = bar_maps.get(code, {}).get(trade_date)
            if bar is None:
                diagnostics["missing_entry_bar"] += 1
                continue
            block_reason = _buy_block_reason(bar)
            if block_reason is not None:
                diagnostics[f"{block_reason}_blocked"] += 1
                continue
            intended_entry = float(bar["open"])
            entry_price = intended_entry * (1.0 + slippage_rate)
            if entry_price <= 0:
                continue
            budget = equity_now * float(settings["position_size_pct"])
            lot_size = int(settings["lot_size"])
            shares = int(budget / (entry_price * lot_size)) * lot_size
            intended_shares = shares
            if bar.get("volume") is not None:
                volume_cap = (
                    int(
                        float(bar["volume"])
                        * float(settings["max_volume_participation"])
                        / lot_size
                    )
                    * lot_size
                )
                shares = min(shares, volume_cap)
                if shares < intended_shares:
                    diagnostics["volume_capped"] += 1
                if shares <= 0:
                    diagnostics["volume_zero_after_cap"] += 1
                    continue
            while shares > 0:
                gross = entry_price * shares
                entry_fees = _buy_fees(gross, settings)
                if gross + entry_fees <= cash:
                    break
                shares -= lot_size
            if shares <= 0:
                diagnostics["cash_blocked"] += 1
                continue
            gross = entry_price * shares
            entry_fees = _buy_fees(gross, settings)
            entry_cost = gross + entry_fees
            cash -= entry_cost
            turnover += gross
            holdings[code] = {
                "code": code,
                "decision_date": signal["decision_date"],
                "entry_date": trade_date,
                "intended_entry": intended_entry,
                "entry_price": entry_price,
                "entry_cost": entry_cost,
                "entry_fees": entry_fees,
                "entry_slippage": abs(entry_price - intended_entry) * shares,
                "shares": shares,
                "atr": float(signal["atr"]),
                "stop_loss": entry_price
                - float(settings["stop_atr_multiple"]) * float(signal["atr"]),
                "take_profit": entry_price
                + float(settings["target_atr_multiple"]) * float(signal["atr"]),
                "hold_sessions": 0,
                "last_price": float(bar["close"]),
                "last_date": trade_date,
                "score": float(signal["score"]),
            }
            latest_close[code] = float(bar["close"])
            equity_now = _mark_to_market(cash, holdings, latest_close)
            diagnostics["filled_entries"] += 1

        for holding in holdings.values():
            if trade_date in bar_maps.get(holding["code"], {}):
                holding["last_price"] = float(latest_close[holding["code"]])
                holding["last_date"] = trade_date
        equity.append(
            _mark_to_market_point(
                trade_date=trade_date,
                cash=cash,
                holdings=holdings,
                latest_close=latest_close,
                realized_pnl=realized_pnl,
                turnover=turnover,
            )
        )

    final_date = all_dates[-1]
    for code in list(holdings):
        holding = holdings[code]
        if final_date <= holding["entry_date"]:
            continue
        bar = bar_maps.get(code, {}).get(final_date)
        if _sell_block_reason(bar) is not None:
            continue
        trade, proceeds = _close_position(
            holding=holding,
            trade_date=final_date,
            intended_exit=float(bar["close"]),
            exit_reason="end_of_backtest",
            config=settings,
        )
        cash += proceeds
        realized_pnl += float(trade["pnl"])
        turnover += float(trade["exit_price"]) * int(trade["shares"])
        trades.append(trade)
        del holdings[code]

    open_positions: list[dict[str, Any]] = []
    for code, holding in sorted(holdings.items()):
        bar = bar_maps.get(code, {}).get(final_date)
        reason = (
            "entry_at_final_session"
            if final_date <= holding["entry_date"]
            else _sell_block_reason(bar)
        )
        if reason is None:
            raise TrialBacktestError(
                f"sellable final-session holding was not liquidated: {code}"
            )
        last_price = float(holding["last_price"])
        cost = float(holding["entry_cost"])
        market_value = last_price * int(holding["shares"])
        open_positions.append(
            {
                "code": code,
                "shares": int(holding["shares"]),
                "last_date": str(holding["last_date"]),
                "last_price": round(last_price, 6),
                "cost": round(cost, 6),
                "unrealized_pnl": round(market_value - cost, 6),
                "unsellable_reason": reason,
            }
        )
    liquidated_position_cost = round(
        sum(float(item["cost"]) for item in open_positions), 6
    )
    liquidated_unrealized_pnl = round(
        sum(float(item["unrealized_pnl"]) for item in open_positions), 6
    )
    liquidated_market_value = round(
        liquidated_position_cost + liquidated_unrealized_pnl, 6
    )
    liquidated_equity = round(cash + liquidated_market_value, 6)
    liquidated_equity_detail = {
        "cash": round(cash, 6),
        "realized_pnl": round(realized_pnl, 6),
        "open_cost": liquidated_position_cost,
        "unrealized_pnl": liquidated_unrealized_pnl,
        "open_market_value": liquidated_market_value,
        "total_assets": liquidated_equity,
        "closed_trade_count": len(trades),
        "open_position_count": len(open_positions),
    }
    trades.sort(key=lambda item: (item["entry_date"], item["exit_date"], item["code"]))
    metrics = _metrics(
        initial_cash=float(settings["initial_cash"]),
        equity=equity,
        trades=trades,
        turnover=turnover,
    )
    metrics.update(
        {
            "execution_price_basis": RAW_PRICE_BASIS,
            "research_price_basis": market_views["research"]["price_basis"],
            "benchmark_price_basis": market_views["research"]["price_basis"],
        }
    )
    closed_trade_stats = _closed_trade_stats(metrics)
    metrics["execution_diagnostics"] = diagnostics
    benchmark = _benchmark_metrics(
        initial_cash=float(settings["initial_cash"]),
        equity=equity,
        market=research_market,
    )
    metrics.update(benchmark)
    metrics["excess_return"] = round(
        float(metrics["total_return"]) - float(metrics["benchmark_total_return"]), 6
    )
    return {
        "config": settings,
        "execution_price_basis": RAW_PRICE_BASIS,
        "research_price_basis": market_views["research"]["price_basis"],
        "return_anomaly_policy": deepcopy(RETURN_ANOMALY_POLICY),
        "mark_to_market_equity": equity,
        "liquidated_equity": liquidated_equity,
        "liquidated_equity_detail": liquidated_equity_detail,
        "closed_trade_stats": closed_trade_stats,
        "open_positions": open_positions,
        "equity": equity,
        "trades": trades,
        "metrics": metrics,
        "start_date": equity[0]["trade_date"] if equity else None,
        "end_date": equity[-1]["trade_date"] if equity else None,
    }


def _resolve_lineage(
    database: str | Path | None,
    *,
    replay_id: str,
    candidate_id: str,
    experiment_id: str | None,
) -> tuple[Path, str, str, dict[str, Any]]:
    replay_id = _required_text(replay_id, "replay_id")
    candidate_id = _required_text(candidate_id, "candidate_id")
    target = initialize_experiments_database(database)
    with _connect(target) as connection:
        replay = connection.execute(
            """
            SELECT candidate_id, artifact_path, artifact_sha256
            FROM score_replays WHERE replay_id=?
            """,
            (replay_id,),
        ).fetchone()
        if replay is None:
            raise TrialBacktestError(f"replay lineage is not recorded: {replay_id}")
        if replay["candidate_id"] != candidate_id:
            raise TrialBacktestError("candidate_id does not match replay lineage")
        if replay["artifact_sha256"] != replay_id:
            raise TrialBacktestError("replay_id does not match its artifact hash")

        if experiment_id is None:
            experiments = connection.execute(
                """
                SELECT experiment_id FROM experiments
                WHERE candidate_id=? AND replay_id=?
                ORDER BY experiment_id
                """,
                (candidate_id, replay_id),
            ).fetchall()
            if not experiments:
                raise TrialBacktestError("experiment lineage is not recorded")
            if len(experiments) != 1:
                raise TrialBacktestError(
                    "multiple experiments match the replay lineage; experiment_id is required"
                )
            resolved_experiment_id = str(experiments[0]["experiment_id"])
        else:
            resolved_experiment_id = _required_text(experiment_id, "experiment_id")
            experiment = connection.execute(
                """
                SELECT candidate_id, replay_id FROM experiments
                WHERE experiment_id=?
                """,
                (resolved_experiment_id,),
            ).fetchone()
            if (
                experiment is None
                or experiment["candidate_id"] != candidate_id
                or experiment["replay_id"] != replay_id
            ):
                raise TrialBacktestError(
                    "experiment_id does not match replay and candidate lineage"
                )

    try:
        payload = load_replay_artifact(
            replay["artifact_path"], expected_sha256=replay["artifact_sha256"]
        )
    except (OSError, ScoreReplayError) as exc:
        raise TrialBacktestError(f"cannot load replay artifact: {exc}") from exc
    if payload.get("candidate_id") != candidate_id:
        raise TrialBacktestError("replay artifact candidate_id does not match candidate_id")
    return target, resolved_experiment_id, replay_id, payload


def initialize_trial_backtest_schema(
    database: str | Path | None = None,
) -> Path:
    target = initialize_experiments_database(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                run_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                candidate_id TEXT NOT NULL REFERENCES model_candidates(candidate_id),
                replay_id TEXT NOT NULL REFERENCES score_replays(replay_id),
                config_hash TEXT NOT NULL,
                code_version TEXT NOT NULL,
                status TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                initial_cash REAL NOT NULL,
                config_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                equity_json TEXT NOT NULL,
                mark_to_market_equity_json TEXT,
                liquidated_equity REAL,
                closed_trade_stats_json TEXT,
                open_positions_json TEXT,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backtest_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
                trade_sequence INTEGER NOT NULL,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                candidate_id TEXT NOT NULL REFERENCES model_candidates(candidate_id),
                replay_id TEXT NOT NULL REFERENCES score_replays(replay_id),
                code TEXT NOT NULL,
                decision_date TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                exit_date TEXT NOT NULL,
                intended_entry REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                pnl REAL NOT NULL,
                return_pct REAL NOT NULL,
                fees REAL NOT NULL,
                slippage REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                hold_sessions INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE(run_id, trade_sequence)
            );

            CREATE INDEX IF NOT EXISTS idx_trial_runs_lineage
                ON backtest_runs(experiment_id, candidate_id, replay_id);
            CREATE INDEX IF NOT EXISTS idx_trial_trades_lineage
                ON backtest_trades(experiment_id, candidate_id, replay_id);
            CREATE INDEX IF NOT EXISTS idx_trial_trades_run
                ON backtest_trades(run_id, entry_date, trade_sequence);
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(backtest_runs)")
        }
        if "config_hash" not in columns:
            connection.execute("ALTER TABLE backtest_runs ADD COLUMN config_hash TEXT")
        for name, declaration in (
            ("mark_to_market_equity_json", "TEXT"),
            ("liquidated_equity", "REAL"),
            ("closed_trade_stats_json", "TEXT"),
            ("open_positions_json", "TEXT"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE backtest_runs ADD COLUMN {name} {declaration}"
                )
        rows = connection.execute(
            """
            SELECT run_id, candidate_id, config_json
            FROM backtest_runs
            WHERE config_hash IS NULL OR config_hash=''
            """
        ).fetchall()
        for row in rows:
            try:
                config = json.loads(row["config_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise TrialBacktestError(
                    f"backtest run has invalid config_json: {row['run_id']}"
                ) from exc
            if not isinstance(config, Mapping):
                raise TrialBacktestError(
                    f"backtest run config_json must be an object: {row['run_id']}"
                )
            connection.execute(
                "UPDATE backtest_runs SET config_hash=? WHERE run_id=?",
                (
                    trial_config_hash(str(row["candidate_id"]), config),
                    row["run_id"],
                ),
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trial_runs_config_hash "
            "ON backtest_runs(config_hash)"
        )
    return target


def _run_id_for(
    *,
    experiment_id: str,
    candidate_id: str,
    replay_id: str,
    bars_sha256: str,
    config: Mapping[str, Any],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "experiment_id": experiment_id,
                "candidate_id": candidate_id,
                "replay_id": replay_id,
                "bars_sha256": bars_sha256,
                "config": dict(config),
            },
            trailing_newline=False,
        )
    )


def _persist_result(target: Path, *, run: Mapping[str, Any]) -> None:
    run_id = str(run["run_id"])
    with _connect(target) as connection:
        existing = connection.execute(
            """
            SELECT experiment_id, candidate_id, replay_id
            FROM backtest_runs WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if existing is not None and (
            existing["experiment_id"] != run["experiment_id"]
            or existing["candidate_id"] != run["candidate_id"]
            or existing["replay_id"] != run["replay_id"]
        ):
            raise TrialBacktestError("run_id is already linked to different lineage")
        connection.execute(
            """
            INSERT INTO backtest_runs (
                run_id, experiment_id, candidate_id, replay_id, config_hash, code_version,
                status, start_date, end_date, initial_cash, config_json,
                metrics_json, equity_json, mark_to_market_equity_json,
                liquidated_equity, closed_trade_stats_json, open_positions_json,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                experiment_id=excluded.experiment_id,
                candidate_id=excluded.candidate_id,
                replay_id=excluded.replay_id,
                config_hash=excluded.config_hash,
                code_version=excluded.code_version,
                status=excluded.status,
                start_date=excluded.start_date,
                end_date=excluded.end_date,
                initial_cash=excluded.initial_cash,
                config_json=excluded.config_json,
                metrics_json=excluded.metrics_json,
                equity_json=excluded.equity_json,
                mark_to_market_equity_json=excluded.mark_to_market_equity_json,
                liquidated_equity=excluded.liquidated_equity,
                closed_trade_stats_json=excluded.closed_trade_stats_json,
                open_positions_json=excluded.open_positions_json
            """,
            (
                run_id,
                run["experiment_id"],
                run["candidate_id"],
                run["replay_id"],
                run["config_hash"],
                run["code_version"],
                run["status"],
                run["start_date"],
                run["end_date"],
                float(run["initial_cash"]),
                canonical_json_bytes(
                    run["config"], trailing_newline=False
                ).decode("utf-8"),
                canonical_json_bytes(
                    run["metrics"], trailing_newline=False
                ).decode("utf-8"),
                canonical_json_bytes(
                    run["equity"], trailing_newline=False
                ).decode("utf-8"),
                canonical_json_bytes(
                    run["mark_to_market_equity"], trailing_newline=False
                ).decode("utf-8"),
                float(run["liquidated_equity"]),
                canonical_json_bytes(
                    run["closed_trade_stats"], trailing_newline=False
                ).decode("utf-8"),
                canonical_json_bytes(
                    run["open_positions"], trailing_newline=False
                ).decode("utf-8"),
                _now_utc(),
            ),
        )
        connection.execute("DELETE FROM backtest_trades WHERE run_id=?", (run_id,))
        for sequence, trade in enumerate(run["trades"]):
            connection.execute(
                """
                INSERT INTO backtest_trades (
                    run_id, trade_sequence, experiment_id, candidate_id, replay_id,
                    code, decision_date, entry_date, exit_date, intended_entry,
                    entry_price, exit_price, shares, stop_loss, take_profit, pnl,
                    return_pct, fees, slippage, exit_reason, hold_sessions,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    run["experiment_id"],
                    run["candidate_id"],
                    run["replay_id"],
                    trade["code"],
                    trade["decision_date"],
                    trade["entry_date"],
                    trade["exit_date"],
                    float(trade["intended_entry"]),
                    float(trade["entry_price"]),
                    float(trade["exit_price"]),
                    int(trade["shares"]),
                    float(trade["stop_loss"]),
                    float(trade["take_profit"]),
                    float(trade["pnl"]),
                    float(trade["return_pct"]),
                    float(trade["fees"]),
                    float(trade["slippage"]),
                    trade["exit_reason"],
                    int(trade["hold_sessions"]),
                    canonical_json_bytes(
                        trade.get("metadata") or {}, trailing_newline=False
                    ).decode("utf-8"),
                ),
            )


def run_trial_backtest(
    database: str | Path | None,
    replay_id: str,
    candidate_id: str,
    bars: Any,
    *,
    research_bars: Any | None = None,
    price_basis: str | None = None,
    research_price_basis: str | None = None,
    experiment_id: str | None = None,
    config: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    code_version: str = "kinace-v3",
) -> dict[str, Any]:
    """Run and persist one candidate/replay/experiment-specific trial backtest."""
    settings = normalize_backtest_config(config)
    market_views = normalize_market_views(
        bars,
        research_bars,
        price_basis=price_basis,
        research_price_basis=research_price_basis,
    )
    target, resolved_experiment_id, resolved_replay_id, payload = _resolve_lineage(
        database,
        replay_id=replay_id,
        candidate_id=candidate_id,
        experiment_id=experiment_id,
    )
    from .synthetic_sandbox import TRACK, active_track

    track = active_track(target)
    scores = _normalize_scores(payload)
    simulation = simulate_trial(
        scores=scores,
        bars=market_views["execution"],
        research_bars=market_views["research"],
        config=settings,
    )

    bars_sha256 = sha256_bytes(
        canonical_json_bytes(market_views, trailing_newline=False)
    )
    resolved_run_id = (
        _required_text(run_id, "run_id")
        if run_id is not None
        else _run_id_for(
            experiment_id=resolved_experiment_id,
            candidate_id=candidate_id,
            replay_id=resolved_replay_id,
            bars_sha256=bars_sha256,
            config=settings,
        )
    )
    if track == TRACK and not resolved_run_id.startswith("sandbox-"):
        resolved_run_id = f"sandbox-{resolved_run_id}"
    run = {
        "run_id": resolved_run_id,
        "track": track,
        "experiment_id": resolved_experiment_id,
        "candidate_id": candidate_id,
        "replay_id": resolved_replay_id,
        "code_version": _required_text(code_version, "code_version"),
        "status": "completed",
        "start_date": simulation["start_date"],
        "end_date": simulation["end_date"],
        "initial_cash": float(settings["initial_cash"]),
        "config": settings,
        "config_hash": trial_config_hash(candidate_id, settings),
        "execution_price_basis": simulation["execution_price_basis"],
        "research_price_basis": simulation["research_price_basis"],
        "return_anomaly_policy": simulation["return_anomaly_policy"],
        "metrics": simulation["metrics"],
        "mark_to_market_equity": simulation["mark_to_market_equity"],
        "liquidated_equity": simulation["liquidated_equity"],
        "liquidated_equity_detail": simulation["liquidated_equity_detail"],
        "closed_trade_stats": simulation["closed_trade_stats"],
        "open_positions": simulation["open_positions"],
        "equity": simulation["mark_to_market_equity"],
        "trades": simulation["trades"],
    }
    initialize_trial_backtest_schema(target)
    _persist_result(target, run=run)
    artifact_dir = target.parent / "backtest_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{resolved_run_id}.json"
    artifact_bytes = canonical_json_bytes(run, trailing_newline=True)
    artifact_path.write_bytes(artifact_bytes)
    artifact_sha256 = sha256_bytes(artifact_bytes)
    evidence_id = record_backtest_evidence(
        target,
        code_version=run["code_version"],
        candidate_id=candidate_id,
        replay_id=resolved_replay_id,
        run_id=resolved_run_id,
        metrics=simulation["metrics"],
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
    )
    run["artifact_path"] = str(artifact_path.resolve(strict=False))
    run["artifact_sha256"] = artifact_sha256
    run["backtest_evidence_id"] = evidence_id
    return deepcopy(run)


__all__ = [
    "DEFAULT_CONFIG",
    "EXECUTION_PRICE_BASIS",
    "MARK_TO_MARKET_EQUITY_BASIS",
    "RAW_PRICE_BASIS",
    "RESEARCH_PRICE_BASIS",
    "RETURN_ANOMALY_POLICY",
    "SCHEMA_VERSION",
    "TrialBacktestError",
    "initialize_trial_backtest_schema",
    "normalize_backtest_config",
    "normalize_market_bars",
    "normalize_market_views",
    "run_trial_backtest",
    "simulate_trial",
    "trial_config_hash",
]
