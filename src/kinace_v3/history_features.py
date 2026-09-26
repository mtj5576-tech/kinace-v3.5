"""Build point-in-time V3 feature snapshots from canonical market revisions."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .data_collection.models import RevisionStatus
from .data_collection.store import RevisionStore
from .feature_snapshot import (
    create_feature_snapshot,
    persist_feature_snapshot,
    write_feature_snapshot,
)
from .pit_universe import (
    HISTORICAL_SNAPSHOT_MODE,
    PitUniverseError,
    resolve_pit_policy,
    select_pit_universe_from_context,
)
from .price_basis import (
    ADJUSTED_PRICE_BASIS,
    RAW_PRICE_BASIS,
    RESEARCH_PRICE_BASES,
    PriceBasisError,
    derive_adjusted_bars,
    normalize_price_basis,
)
from .research_labeling import build_forward_labels


SCHEMA_VERSION = 1
DEFAULT_MODEL_CONFIG = {
    "version": "kinace-v3.2-history-v1",
    "mode": "manual_baseline",
    "weights": {
        "rel_sector_strength": 0.22,
        "close_location_5": 0.17,
        "volume_ratio": 0.17,
        "log_amount": 0.17,
        "turnover": 0.00,
        "proximity_20d_high": 0.11,
        "ret_5": 0.11,
        "gap": 0.05,
    },
    "standardization": {
        "version": 1,
        "method": "mad_winsorized",
        "mad_multiplier": 3.0,
        "neutralize_industry": False,
        "neutralize_market_cap": False,
    },
    "penalties": {
        "rsi_overbought": 80.0,
        "kdj_overbought": 100.0,
        "overheat_penalty": 1.0,
        "amplitude_threshold": 0.04,
        "amplitude_multiplier": 4.0,
        "amplitude_max_penalty": 0.35,
    },
}


class HistoryFeatureError(ValueError):
    """Raised when historical feature snapshots cannot be built safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("record")
    return value if isinstance(value, Mapping) else row


def _key(record: Mapping[str, Any], date_field: str) -> tuple[str, str]:
    return str(record.get("ts_code") or ""), str(record.get(date_field) or "")


def _bare_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0] if "." in text else text


def _iter_current_dataset_rows(
    database: str | Path,
    dataset: str,
    *,
    date_field: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Read only current, non-superseded rows for one dataset."""
    clauses = [
        "r.dataset=?",
        "r.status <> 'superseded'",
        (
            "NOT EXISTS (SELECT 1 FROM data_revisions child "
            "WHERE child.supersedes_revision_id=r.revision_id "
            "AND child.status <> 'superseded')"
        ),
        (
            "NOT EXISTS (SELECT 1 FROM data_revisions newer "
            "WHERE newer.dataset=r.dataset "
            "AND newer.natural_key_json=r.natural_key_json "
            "AND newer.revision_sequence > r.revision_sequence "
            "AND newer.status <> 'superseded')"
        ),
    ]
    params: list[Any] = [dataset]
    if date_field and start_date:
        clauses.append(f"json_extract(r.record_json, '$.{date_field}') >= ?")
        params.append(start_date)
    if date_field and end_date:
        clauses.append(f"json_extract(r.record_json, '$.{date_field}') <= ?")
        params.append(end_date)
    sql = (
        "SELECT r.status, r.record_json, r.observed_at_utc "
        "FROM data_revisions r WHERE "
        + " AND ".join(clauses)
        + " ORDER BY r.natural_key_json"
    )
    with sqlite3.connect(str(database)) as connection:
        for row in connection.execute(sql, params):
            record = json.loads(row[1])
            status = str(row[0])
            yield {
                "dataset": dataset,
                "status": status,
                "effective_status": status,
                "observed_at_utc": row[2],
                "record": record,
                "payload": record,
            }


def _current_dataset_rows(
    database: str | Path,
    dataset: str,
    *,
    date_field: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    return list(
        _iter_current_dataset_rows(
            database,
            dataset,
            date_field=date_field,
            start_date=start_date,
            end_date=end_date,
        )
    )


def build_history_pit_context(
    *,
    daily_rows: Sequence[Mapping[str, Any]],
    metadata_rows: Sequence[Mapping[str, Any]],
    state_rows: Sequence[Mapping[str, Any]],
    membership_rows: Sequence[Mapping[str, Any]],
    price_basis: str = RAW_PRICE_BASIS,
) -> dict[str, Any]:
    try:
        resolved_basis = normalize_price_basis(price_basis)
    except PriceBasisError as exc:
        raise HistoryFeatureError(str(exc)) from exc
    if resolved_basis != RAW_PRICE_BASIS:
        raise HistoryFeatureError("PIT filtering requires price_basis=raw")
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        record = _record(row)
        by_code[str(record.get("ts_code") or "")].append(record)
    for rows in by_code.values():
        rows.sort(key=lambda item: str(item.get("trade_date") or ""))
    return {
        "price_basis": resolved_basis,
        "metadata": {
            _bare_code(_record(row).get("ts_code")): _record(row)
            for row in metadata_rows
            if _bare_code(_record(row).get("ts_code"))
        },
        "daily": by_code,
        "states": {
            (
                _bare_code(_record(row).get("ts_code")),
                str(_record(row).get("trade_date") or ""),
            ): _record(row)
            for row in state_rows
        },
        "memberships": [_record(row) for row in membership_rows],
    }


def load_history_pit_context(
    database: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    price_basis: str = RAW_PRICE_BASIS,
) -> dict[str, Any]:
    daily_rows = [
        row
        for row in _current_dataset_rows(
            database,
            "daily_bars",
            date_field="trade_date",
            end_date=end_date,
        )
        if str(row.get("effective_status") or row.get("status"))
        in {RevisionStatus.OBSERVED.value, RevisionStatus.BACKFILLED_RELIABLE.value}
    ]
    return build_history_pit_context(
        daily_rows=daily_rows,
        metadata_rows=_current_dataset_rows(database, "stock_metadata"),
        state_rows=_current_dataset_rows(
            database,
            "security_state",
            date_field="trade_date",
            start_date=start_date,
            end_date=end_date,
        ),
        membership_rows=_current_dataset_rows(database, "sector_membership"),
        price_basis=price_basis,
    )


def load_compact_history_pit_context(
    database: str | Path,
    *,
    decision_dates: Sequence[str],
    end_date: str | None = None,
    price_basis: str = RAW_PRICE_BASIS,
) -> dict[str, Any]:
    try:
        resolved_basis = normalize_price_basis(price_basis)
    except PriceBasisError as exc:
        raise HistoryFeatureError(str(exc)) from exc
    if resolved_basis != RAW_PRICE_BASIS:
        raise HistoryFeatureError("PIT filtering requires price_basis=raw")
    decisions = {str(value) for value in decision_dates if str(value)}
    if not decisions:
        raise HistoryFeatureError("decision_dates must not be empty")

    history_counts: dict[str, int] = defaultdict(int)
    latest_closes: dict[str, float] = {}
    recent_amounts: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))
    daily_series: dict[str, list[tuple[str, int, float | None, float]]] = defaultdict(list)
    for row in _iter_current_dataset_rows(
        database,
        "daily_bars",
        date_field="trade_date",
        end_date=end_date,
    ):
        if str(row.get("effective_status") or row.get("status")) not in {
            RevisionStatus.OBSERVED.value,
            RevisionStatus.BACKFILLED_RELIABLE.value,
        }:
            continue
        record = _record(row)
        code = _bare_code(record.get("ts_code"))
        trade_date = str(record.get("trade_date") or "")
        history_counts[code] += 1
        latest_closes[code] = float(record.get("close") or 0.0)
        amount = float(record.get("amount") or 0.0)
        if amount <= 0:
            amount = float(record.get("vol") or 0.0) * statistics.fmean(
                (
                    float(record.get("open") or 0.0),
                    float(record.get("high") or 0.0),
                    float(record.get("low") or 0.0),
                    float(record.get("close") or 0.0),
                )
            )
        recent_amounts[code].append(amount)
        daily_series[code].append(
            (
                trade_date,
                history_counts[code],
                latest_closes.get(code),
                statistics.fmean(recent_amounts[code]) if recent_amounts[code] else 0.0,
            )
        )

    metadata = {
        _bare_code(_record(row).get("ts_code")): _record(row)
        for row in _iter_current_dataset_rows(database, "stock_metadata")
        if _bare_code(_record(row).get("ts_code"))
    }
    states = {
        (
            _bare_code(_record(row).get("ts_code")),
            str(_record(row).get("trade_date") or ""),
        ): _record(row)
        for row in _iter_current_dataset_rows(
            database,
            "security_state",
            date_field="trade_date",
            start_date=min(decisions),
            end_date=max(decisions),
        )
        if str(_record(row).get("trade_date") or "") in decisions
    }
    return {
        "price_basis": resolved_basis,
        "metadata": metadata,
        "daily_summary": daily_series,
        "states": states,
        "memberships": [
            _record(row) for row in _iter_current_dataset_rows(database, "sector_membership")
        ],
    }


def _sma(values: Sequence[float], period: int) -> float:
    window = values[-period:] if len(values) >= period else values
    return sum(window) / len(window) if window else 0.0


def _rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = [max(values[index] - values[index - 1], 0.0) for index in range(1, len(values))]
    losses = [max(values[index - 1] - values[index], 0.0) for index in range(1, len(values))]
    average_gain = sum(gains[-period:]) / period
    average_loss = sum(losses[-period:]) / period
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


def _kdj_j(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 9,
) -> float:
    if len(closes) < period:
        return 50.0
    k = 50.0
    d = 50.0
    for index in range(period - 1, len(closes)):
        highest = max(highs[index - period + 1 : index + 1])
        lowest = min(lows[index - period + 1 : index + 1])
        rsv = (
            50.0
            if highest == lowest
            else (closes[index] - lowest) / (highest - lowest) * 100.0
        )
        k = 2.0 / 3.0 * k + 1.0 / 3.0 * rsv
        d = 2.0 / 3.0 * d + 1.0 / 3.0 * k
    return 3.0 * k - 2.0 * d


def _candidate_features(
    rows: Sequence[Mapping[str, Any]],
    index: int,
    *,
    turnover: float | None,
    label: Mapping[str, Any] | None,
    observed_at_utc: str | None,
    price_basis: str,
) -> dict[str, Any]:
    history = rows[: index + 1]
    closes = [float(row["close"]) for row in history]
    opens = [float(row["open"]) for row in history]
    highs = [float(row["high"]) for row in history]
    lows = [float(row["low"]) for row in history]
    volumes = [float(row.get("vol") or 0.0) for row in history]
    amounts = [float(row.get("amount") or 0.0) for row in history]
    close = closes[-1]
    previous_close = closes[-2] if len(closes) > 1 else close
    ret_5 = close / closes[-6] - 1.0 if len(closes) >= 6 and closes[-6] else None
    recent_low = min(lows[-5:])
    recent_high = max(highs[-5:])
    close_location = (
        (close - recent_low) / (recent_high - recent_low)
        if recent_high > recent_low
        else 0.5
    )
    high_20 = max(highs[-20:])
    volume_average = _sma(volumes, 5)
    amount = amounts[-1]
    if amount <= 0:
        amount = volumes[-1] * statistics.fmean(
            (float(history[-1]["open"]), float(history[-1]["high"]), float(history[-1]["low"]), close)
        )
    amplitude_5d = statistics.fmean(
        (high - low) / c if c else 0.0
        for high, low, c in zip(highs[-5:], lows[-5:], closes[-5:])
    )
    raw_inputs = {
        "rel_sector_strength": None,
        "close_location_5": close_location,
        "volume_ratio": volumes[-1] / volume_average if volume_average else None,
        "log_amount": math.log10(amount) if amount > 0 else None,
        "turnover": turnover,
        "proximity_20d_high": close / high_20 if high_20 else None,
        "ret_5": ret_5,
        "gap": opens[-1] / previous_close - 1.0 if previous_close else None,
    }
    raw_inputs["turnover_source"] = (
        "canonical" if turnover is not None else "unavailable_zero_weight"
    )
    multi_horizon_labels = build_forward_labels(
        rows,
        index,
        price_basis=price_basis,
    )
    if not multi_horizon_labels and label is not None:
        stored_basis = label.get("price_basis")
        if stored_basis is not None and normalize_price_basis(
            stored_basis, field="forward_labels.price_basis"
        ) == price_basis:
            multi_horizon_labels = dict(label)
    return {
        "code": str(history[-1]["ts_code"]),
        "raw_inputs": raw_inputs,
        "penalty_inputs": {
            "rsi": _rsi(closes, 14),
            "kdj_j": _kdj_j(highs, lows, closes, 9),
            "amplitude_5d": amplitude_5d,
        },
        "labels": multi_horizon_labels,
        "point_in_time": {
            "as_of_utc": observed_at_utc or _utc_now(),
        },
        "source": "canonical_market_revisions_research_adjusted",
        "price_basis": price_basis,
    }


def build_history_snapshots(
    store: RevisionStore | str | Path,
    *,
    output_dir: str | Path,
    database: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_history: int = 65,
    model_config: Mapping[str, Any] | None = None,
    data_gate: Mapping[str, Any] | None = None,
    pit_policy: Mapping[str, Any] | None = None,
    pit_policy_path: str | Path | None = None,
    snapshot_mode: str = HISTORICAL_SNAPSHOT_MODE,
    price_basis: str = ADJUSTED_PRICE_BASIS,
) -> dict[str, Any]:
    """Build one immutable V3 snapshot per decision date from real revisions."""
    try:
        resolved_price_basis = normalize_price_basis(price_basis)
    except PriceBasisError as exc:
        raise HistoryFeatureError(str(exc)) from exc
    if resolved_price_basis not in RESEARCH_PRICE_BASES:
        raise HistoryFeatureError(
            "historical features and labels require adjusted or total_return price_basis"
        )
    resolved_store = store if isinstance(store, RevisionStore) else RevisionStore(store)
    resolved_database = Path(database).resolve(strict=False) if database else resolved_store.database
    resolved_output = Path(output_dir).resolve(strict=False)
    try:
        policy_resolution = resolve_pit_policy(
            pit_policy,
            policy_path=pit_policy_path,
            mode=snapshot_mode,
        )
    except PitUniverseError as exc:
        raise HistoryFeatureError(str(exc)) from exc
    resolved_pit_policy = policy_resolution["policy"]
    resolved_output.mkdir(parents=True, exist_ok=True)
    resolved_min_history = int(min_history)
    if resolved_min_history < 20:
        raise HistoryFeatureError("min_history must be at least 20 sessions")

    daily_rows = [
        row
        for row in _current_dataset_rows(
            resolved_database,
            "daily_bars",
            date_field="trade_date",
        )
        if str(row.get("effective_status") or row.get("status"))
        in {RevisionStatus.OBSERVED.value, RevisionStatus.BACKFILLED_RELIABLE.value}
    ]
    adjustment_rows = _current_dataset_rows(
        resolved_database,
        "adjustment_factors",
    )
    metadata_rows = _current_dataset_rows(resolved_database, "stock_metadata")
    turnover_rows = _current_dataset_rows(
        resolved_database,
        "daily_basic_metrics",
        date_field="trade_date",
        start_date=start_date,
        end_date=end_date,
    )
    turnover_lookup = {
        _key(_record(row), "trade_date"): _record(row).get("turnover_rate")
        for row in turnover_rows
    }
    label_rows = _current_dataset_rows(
        resolved_database,
        "forward_labels",
        date_field="decision_date",
        start_date=start_date,
        end_date=end_date,
    )
    labels = {
        _key(_record(row), "decision_date"): dict(_record(row))
        for row in label_rows
    }
    state_rows = _current_dataset_rows(
        resolved_database,
        "security_state",
        date_field="trade_date",
        start_date=start_date,
        end_date=end_date,
    )
    membership_rows = _current_dataset_rows(resolved_database, "sector_membership")

    raw_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        record = dict(_record(row))
        record["_observed_at_utc"] = row.get("observed_at_utc")
        raw_by_code[str(record.get("ts_code") or "")].append(record)
    for rows in raw_by_code.values():
        rows.sort(key=lambda item: str(item.get("trade_date") or ""))
    try:
        research_series = derive_adjusted_bars(
            raw_by_code,
            adjustment_rows,
            output_basis=resolved_price_basis,
        )
    except PriceBasisError as exc:
        raise HistoryFeatureError(str(exc)) from exc
    by_code: dict[str, list[dict[str, Any]]] = research_series["bars"]

    candidates_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for code, rows in by_code.items():
        for index in range(resolved_min_history - 1, len(rows)):
            trade_date = str(rows[index].get("trade_date") or "")
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            candidate = _candidate_features(
                rows,
                index,
                turnover=turnover_lookup.get((code, trade_date)),
                label=labels.get((code, trade_date)),
                observed_at_utc=rows[index].get("_observed_at_utc"),
                price_basis=resolved_price_basis,
            )
            candidate["industry"] = "__unavailable_zero__"
            candidates_by_date[trade_date].append(candidate)

    snapshot_reports: list[dict[str, Any]] = []
    pit_context = build_history_pit_context(
        daily_rows=daily_rows,
        metadata_rows=metadata_rows,
        state_rows=state_rows,
        membership_rows=membership_rows,
        price_basis=RAW_PRICE_BASIS,
    )
    for decision_date in sorted(candidates_by_date):
        candidates = candidates_by_date[decision_date]
        universe_selection = select_pit_universe_from_context(
            pit_context,
            decision_date=decision_date,
            policy=resolved_pit_policy,
        )
        eligible = set(universe_selection["eligible_codes"])
        candidates = [candidate for candidate in candidates if candidate["code"] in eligible]
        for candidate in candidates:
            candidate["industry"] = universe_selection["industry_by_code"].get(
                candidate["code"], "__unavailable_zero__"
            )
        candidate_count = int(universe_selection["candidate_count"])
        industry_covered = (
            candidate_count
            if resolved_pit_policy["industry_mode"] == "zero"
            else len(universe_selection["industry_by_code"])
        )
        industry_coverage = (
            round(industry_covered / candidate_count, 8) if candidate_count else 0.0
        )
        industry_returns: dict[str, list[float]] = defaultdict(list)
        for candidate in candidates:
            ret_5 = candidate["raw_inputs"].get("ret_5")
            if ret_5 is not None and candidate.get("industry") != "__unavailable_zero__":
                industry_returns[str(candidate.get("industry") or "unknown")].append(float(ret_5))
        industry_means = {
            industry: statistics.fmean(values)
            for industry, values in industry_returns.items()
            if values
        }
        for candidate in candidates:
            ret_5 = candidate["raw_inputs"].get("ret_5")
            industry = str(candidate.get("industry") or "unknown")
            if industry == "__unavailable_zero__":
                candidate["raw_inputs"]["rel_sector_strength"] = 0.0
                candidate["raw_inputs"]["rel_sector_strength_source"] = "unavailable_zero"
            else:
                candidate["raw_inputs"]["rel_sector_strength_source"] = (
                    "industry_mean" if ret_5 is not None else "industry_return_unavailable"
                )
                candidate["raw_inputs"]["rel_sector_strength"] = (
                    float(ret_5) - industry_means.get(industry, 0.0)
                    if ret_5 is not None
                    else None
                )
            candidate.pop("industry", None)

        snapshot_pit_ready = bool(policy_resolution["pit_ready"] and candidates)
        payload = create_feature_snapshot(
            decision_date=decision_date,
            candidates=candidates,
            model_config=model_config or DEFAULT_MODEL_CONFIG,
            data_gate=data_gate or {
                "status": "history_backfill",
                "ready": False,
                "parity_claimed": False,
            },
        )
        for row in payload["rows"]:
            row["price_basis"] = resolved_price_basis
        payload.update(
            {
                "snapshot_mode": policy_resolution["mode"],
                "pit_policy": resolved_pit_policy,
                "pit_policy_hash": policy_resolution["pit_policy_hash"],
                "industry_source": policy_resolution["industry_source"],
                "industry_coverage": industry_coverage,
                "industry_lookahead_safe": policy_resolution[
                    "industry_lookahead_safe"
                ],
                "pit_ready": snapshot_pit_ready,
                "price_basis": resolved_price_basis,
                "execution_price_basis": RAW_PRICE_BASIS,
                "label_price_basis": resolved_price_basis,
                "adjustment_factor_count": len(adjustment_rows),
            }
        )
        written = write_feature_snapshot(
            payload,
            resolved_output / f"{decision_date}.json",
        )
        persist_feature_snapshot(written, resolved_database)
        snapshot_reports.append(
            {
                "decision_date": decision_date,
                "snapshot_id": written["snapshot_id"],
                "artifact_path": written["artifact_path"],
                "row_count": payload["row_count"],
                "replay_ready_rows": sum(
                    bool(row.get("replay_ready")) for row in payload["rows"]
                ),
                "universe": {
                    "candidate_count": universe_selection["candidate_count"],
                    "eligible_count": universe_selection["eligible_count"],
                    "excluded_count": universe_selection["excluded_count"],
                    "index_snapshot_dates": universe_selection["index_snapshot_dates"],
                    "industry_mode": universe_selection["policy"]["industry_mode"],
                    "industry_lookahead_safe": universe_selection[
                        "industry_lookahead_safe"
                    ],
                },
                "industry_source": policy_resolution["industry_source"],
                "industry_coverage": industry_coverage,
                "industry_lookahead_safe": policy_resolution[
                    "industry_lookahead_safe"
                ],
                "pit_ready": snapshot_pit_ready,
                "price_basis": resolved_price_basis,
                "execution_price_basis": RAW_PRICE_BASIS,
            }
        )

    manifest = resolved_output / "manifest.json"
    existing_snapshots: list[dict[str, Any]] = []
    existing: Mapping[str, Any] | None = None
    if manifest.is_file():
        try:
            existing = json.loads(manifest.read_text(encoding="utf-8"))
            raw_existing = existing.get("snapshots") if isinstance(existing, Mapping) else None
            same_policy = (
                isinstance(existing, Mapping)
                and existing.get("pit_policy_hash")
                == policy_resolution["pit_policy_hash"]
                and existing.get("snapshot_mode") == policy_resolution["mode"]
                and existing.get("industry_source")
                == policy_resolution["industry_source"]
            )
            if same_policy and isinstance(raw_existing, list):
                existing_snapshots = [
                    dict(item) for item in raw_existing if isinstance(item, Mapping)
                ]
        except (OSError, json.JSONDecodeError):
            existing_snapshots = []
    by_date = {
        str(item.get("decision_date") or ""): item
        for item in existing_snapshots
        if str(item.get("decision_date") or "")
    }
    for item in snapshot_reports:
        by_date[str(item["decision_date"])] = item
    merged_snapshots = [by_date[key] for key in sorted(by_date)]
    merged_row_count = sum(int(item.get("row_count") or 0) for item in merged_snapshots)
    merged_industry_covered = sum(
        round(
            float(item.get("industry_coverage") or 0.0)
            * int(item.get("row_count") or 0)
        )
        for item in merged_snapshots
    )
    manifest_pit_ready = bool(
        policy_resolution["pit_ready"]
        and merged_snapshots
        and all(
            item.get("pit_ready") is True
            and item.get("industry_lookahead_safe") is True
            for item in merged_snapshots
        )
    )

    report = {
        "artifact_type": "kinace_v3_history_feature_build",
        "schema_version": SCHEMA_VERSION,
        "database": str(resolved_database),
        "output_dir": str(resolved_output),
        "date_range": {"start": start_date, "end": end_date},
        "min_history": resolved_min_history,
        "snapshot_mode": policy_resolution["mode"],
        "pit_policy": resolved_pit_policy,
        "pit_policy_path": policy_resolution["policy_path"],
        "pit_policy_hash": policy_resolution["pit_policy_hash"],
        "industry_source": policy_resolution["industry_source"],
        "industry_coverage": (
            round(merged_industry_covered / merged_row_count, 8)
            if merged_row_count
            else 0.0
        ),
        "industry_lookahead_safe": policy_resolution["industry_lookahead_safe"],
        "pit_ready": manifest_pit_ready,
        "price_basis": resolved_price_basis,
        "execution_price_basis": RAW_PRICE_BASIS,
        "label_price_basis": resolved_price_basis,
        "adjustment_factor_count": len(adjustment_rows),
        "snapshot_count": len(merged_snapshots),
        "row_count": sum(int(item.get("row_count") or 0) for item in merged_snapshots),
        "replay_ready_rows": sum(
            int(item.get("replay_ready_rows") or 0) for item in merged_snapshots
        ),
        "snapshots": merged_snapshots,
        "completed_at_utc": _utc_now(),
    }
    temporary = manifest.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(manifest)
    report["manifest_path"] = str(manifest)
    return report


__all__ = [
    "DEFAULT_MODEL_CONFIG",
    "HistoryFeatureError",
    "build_history_snapshots",
]
