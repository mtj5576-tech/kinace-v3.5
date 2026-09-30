"""Real-market incremental sync and cross-source parity checks for V3.2."""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .data_collection.ingest import ingest_records
from .data_collection.models import (
    DailyBarRecord,
    DailyBasicMetricRecord,
    ForwardLabelRecord,
    RevisionStatus,
    SecurityStateRecord,
)
from .data_collection.providers.tencent_provider import MarketBundle, TencentMarketProvider
from .data_collection.store import RevisionStore


SCHEMA_VERSION = 1
MAX_TOTAL_MISMATCH_RATIO = 0.02
MAX_VOLUME_MISMATCH_RATIO = 0.005
MAX_CLOSE_RETURN_DELTA = 0.05
MAX_SHAPE_DELTA = 0.05


class MarketSyncError(ValueError):
    """Raised when a real-market sync would violate source or parity policy."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    record = row.get("record")
    return record if isinstance(record, Mapping) else row


def _daily_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return str(record.get("ts_code") or ""), str(record.get("trade_date") or "")


def _key_text(key: Mapping[str, Any]) -> str:
    return json.dumps(dict(key), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _missing_records(store: RevisionStore, dataset: str, records: Sequence[Any]) -> list[Any]:
    keys: set[str] = set()
    for row in store.current_records(dataset):
        record = _record_payload(row)
        if dataset in {"trade_calendar"}:
            key = {"exchange": record.get("exchange"), "cal_date": record.get("cal_date")}
        elif dataset == "forward_labels":
            key = {"ts_code": record.get("ts_code"), "decision_date": record.get("decision_date")}
        else:
            key = {"ts_code": record.get("ts_code"), "trade_date": record.get("trade_date")}
        keys.add(_key_text(key))
    result: list[Any] = []
    for record in records:
        key = record.natural_key()
        if _key_text(key) not in keys:
            result.append(record)
    return result


def _current_rows_by_keys(
    store: RevisionStore,
    dataset: str,
    natural_keys: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    keys = sorted({_key_text(key) for key in natural_keys})
    if not keys:
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    with sqlite3.connect(str(store.database)) as connection:
        connection.row_factory = sqlite3.Row
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT natural_key_json, revision_sequence, status, record_json "
                f"FROM data_revisions WHERE dataset=? "
                f"AND natural_key_json IN ({placeholders})",
                (dataset, *chunk),
            ).fetchall()
            for row in rows:
                key = str(row["natural_key_json"])
                current = result.get(key)
                if current is not None and int(current["revision_sequence"]) >= int(
                    row["revision_sequence"]
                ):
                    continue
                try:
                    record = json.loads(str(row["record_json"]))
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                result[key] = {
                    "record": dict(record),
                    "status": row["status"],
                    "effective_status": row["status"],
                    "revision_sequence": int(row["revision_sequence"]),
                }
    return result


def _missing_indexed_records(
    store: RevisionStore,
    dataset: str,
    records: Sequence[Any],
) -> list[Any]:
    existing = _current_rows_by_keys(
        store,
        dataset,
        [record.natural_key() for record in records],
    )
    return [
        record
        for record in records
        if _key_text(record.natural_key()) not in existing
    ]


def _difference(
    primary: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any] | None:
    fields = ("close_return", "open_to_close", "high_to_close", "low_to_close", "vol")
    mismatches: dict[str, Any] = {}
    for field in fields:
        left = primary.get(field)
        right = incoming.get(field)
        if left is None or right is None:
            continue
        left_value = float(left)
        right_value = float(right)
        if field == "vol":
            scale = max(abs(left_value), abs(right_value), 1.0)
            delta = abs(left_value - right_value) / scale
            if delta > 0.001:
                mismatches[field] = {
                    "primary": left_value,
                    "incoming": right_value,
                    "relative_delta": delta,
                }
            continue
        delta = abs(left_value - right_value)
        if delta > 0.003:
            mismatches[field] = {
                "primary": left_value,
                "incoming": right_value,
                "absolute_delta": delta,
            }
    return mismatches or None


def _price_metrics(records: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    by_code: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_code.setdefault(str(record.get("ts_code") or ""), []).append(record)
    result: dict[tuple[str, str], dict[str, float]] = {}
    for code, rows in by_code.items():
        ordered = sorted(rows, key=lambda item: str(item.get("trade_date") or ""))
        previous_close: float | None = None
        for row in ordered:
            close = float(row.get("close") or 0.0)
            if close <= 0:
                continue
            metrics = {
                "open_to_close": float(row.get("open") or 0.0) / close,
                "high_to_close": float(row.get("high") or 0.0) / close,
                "low_to_close": float(row.get("low") or 0.0) / close,
                "vol": float(row.get("vol") or 0.0),
            }
            if previous_close and previous_close > 0:
                metrics["close_return"] = close / previous_close - 1.0
            result[(code, str(row.get("trade_date") or ""))] = metrics
            previous_close = close
    return result


def _parity_report(
    store: RevisionStore,
    bundle: MarketBundle,
    *,
    incoming_source: str,
    checked_at_utc: str,
) -> dict[str, Any]:
    primary = {
        _daily_key(_record_payload(row)): dict(_record_payload(row))
        for row in store.current_records("daily_bars")
        if str(row.get("effective_status") or row.get("status"))
        in {RevisionStatus.OBSERVED.value, RevisionStatus.BACKFILLED_RELIABLE.value}
    }
    primary_metrics = _price_metrics(list(primary.values()))
    incoming_metrics = _price_metrics([record.to_dict() for record in bundle.daily_bars])
    incoming_by_code: dict[str, list[str]] = {}
    for record in bundle.daily_bars:
        incoming_by_code.setdefault(record.ts_code, []).append(record.trade_date)
    incoming_previous_date: dict[tuple[str, str], str] = {}
    for code, dates in incoming_by_code.items():
        ordered_dates = sorted(set(dates))
        for previous_date, trade_date in zip(ordered_dates, ordered_dates[1:]):
            incoming_previous_date[(code, trade_date)] = previous_date
    overlap = 0
    mismatches: list[dict[str, Any]] = []
    volume_mismatches = 0
    max_delta: dict[str, float] = {}
    for record in bundle.daily_bars:
        incoming = record.to_dict()
        key = _daily_key(incoming)
        existing = primary.get(key)
        if existing is None:
            continue
        overlap += 1
        difference = _difference(
            primary_metrics.get(key, {}),
            incoming_metrics.get(key, {}),
        )
        if difference is not None and "close_return" in difference:
            previous_date = incoming_previous_date.get(key)
            if previous_date is None or (key[0], previous_date) not in primary:
                difference = {
                    field: values
                    for field, values in difference.items()
                    if field != "close_return"
                }
                if not difference:
                    continue
        if difference is None:
            continue
        if "vol" in difference:
            volume_mismatches += 1
        mismatches.append({"ts_code": key[0], "trade_date": key[1], "fields": difference})
        for field, values in difference.items():
            delta = float(values.get("absolute_delta") or values.get("relative_delta") or 0.0)
            max_delta[field] = max(max_delta.get(field, 0.0), delta)
    mismatch_ratio = len(mismatches) / overlap if overlap else 0.0
    volume_mismatch_ratio = volume_mismatches / overlap if overlap else 0.0
    close_return_delta = float(max_delta.get("close_return") or 0.0)
    shape_delta = max(
        float(max_delta.get(field) or 0.0)
        for field in ("open_to_close", "high_to_close", "low_to_close")
    )
    passed = (
        mismatch_ratio <= MAX_TOTAL_MISMATCH_RATIO
        and volume_mismatch_ratio <= MAX_VOLUME_MISMATCH_RATIO
        and close_return_delta <= MAX_CLOSE_RETURN_DELTA
        and shape_delta <= MAX_SHAPE_DELTA
    )
    return {
        "artifact_type": "kinace_v3_source_parity",
        "schema_version": SCHEMA_VERSION,
        "primary_source": "current_v3_revisions",
        "incoming_source": incoming_source,
        "checked_at_utc": checked_at_utc,
        "overlap_records": overlap,
        "mismatch_records": len(mismatches),
        "mismatch_ratio": mismatch_ratio,
        "volume_mismatch_records": volume_mismatches,
        "volume_mismatch_ratio": volume_mismatch_ratio,
        "mismatches": mismatches[:200],
        "max_delta": max_delta,
        "comparison_policy": {
            "absolute_prices_compared": False,
            "price_metrics": [
                "close_return",
                "open_to_close",
                "high_to_close",
                "low_to_close",
            ],
            "volume_normalized_to_shares": True,
        },
        "passed": passed,
        "status": "passed" if passed else "failed",
        "acceptance_policy": {
            "max_total_mismatch_ratio": MAX_TOTAL_MISMATCH_RATIO,
            "max_volume_mismatch_ratio": MAX_VOLUME_MISMATCH_RATIO,
            "max_close_return_delta": MAX_CLOSE_RETURN_DELTA,
            "max_shape_delta": MAX_SHAPE_DELTA,
        },
        "policy": {
            "model_outputs_used_as_labels": False,
            "provisional_records_eligible": False,
            "release_requires_real_complete_records": True,
        },
    }


def _price_scales(store: RevisionStore, bundle: MarketBundle) -> dict[str, float]:
    return _price_scales_from_primary(
        bundle,
        {
        _daily_key(_record_payload(row)): _record_payload(row)
        for row in store.current_records("daily_bars")
        },
    )


def _price_scales_from_primary(
    bundle: MarketBundle,
    primary: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, float]:
    latest: dict[str, tuple[str, float]] = {}
    for record in bundle.daily_bars:
        key = (record.ts_code, record.trade_date)
        existing = primary.get(key)
        if existing is None or record.close <= 0:
            continue
        scale = float(existing.get("close") or 0.0) / record.close
        if scale <= 0:
            continue
        previous = latest.get(record.ts_code)
        if previous is None or record.trade_date > previous[0]:
            latest[record.ts_code] = (record.trade_date, scale)
    return {code: value[1] for code, value in latest.items()}


def _volume_scales(store: RevisionStore, bundle: MarketBundle) -> dict[str, float]:
    return _volume_scales_from_primary(
        bundle,
        {
        _daily_key(_record_payload(row)): _record_payload(row)
        for row in store.current_records("daily_bars")
        },
    )


def _volume_scales_from_primary(
    bundle: MarketBundle,
    primary: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, float]:
    ratios: dict[str, list[float]] = {}
    for record in bundle.daily_bars:
        existing = primary.get((record.ts_code, record.trade_date))
        incoming_volume = float(record.vol or 0.0)
        primary_volume = float((existing or {}).get("vol") or 0.0)
        if incoming_volume <= 0 or primary_volume <= 0:
            continue
        ratios.setdefault(record.ts_code, []).append(primary_volume / incoming_volume)
    return {
        code: float(statistics.median(values))
        for code, values in ratios.items()
        if values
    }


def _scaled(value: float | None, scale: float) -> float | None:
    return None if value is None else float(value) * scale


def _normalize_bundle(
    bundle: MarketBundle,
    scales: Mapping[str, float],
    volume_scales: Mapping[str, float],
) -> MarketBundle:
    if not scales and not volume_scales:
        return bundle
    bars: list[DailyBarRecord] = []
    basics: list[DailyBasicMetricRecord] = []
    states: list[SecurityStateRecord] = []
    labels: list[ForwardLabelRecord] = []
    for record in bundle.daily_bars:
        scale = float(scales.get(record.ts_code, 1.0))
        volume_scale = float(volume_scales.get(record.ts_code, 1.0))
        bars.append(
            DailyBarRecord(
                ts_code=record.ts_code,
                trade_date=record.trade_date,
                open=record.open * scale,
                high=record.high * scale,
                low=record.low * scale,
                close=record.close * scale,
                pre_close=_scaled(record.pre_close, scale),
                change=_scaled(record.change, scale),
                pct_chg=record.pct_chg,
                vol=(record.vol * volume_scale if record.vol is not None else None),
                amount=record.amount,
            )
        )
    for record in bundle.daily_basic_metrics:
        scale = float(scales.get(record.ts_code, 1.0))
        basics.append(
            DailyBasicMetricRecord(
                ts_code=record.ts_code,
                trade_date=record.trade_date,
                close=_scaled(record.close, scale),
                turnover_rate=record.turnover_rate,
                turnover_rate_f=record.turnover_rate_f,
                volume_ratio=record.volume_ratio,
                pe=record.pe,
                pe_ttm=record.pe_ttm,
                pb=record.pb,
                ps=record.ps,
                ps_ttm=record.ps_ttm,
                dv_ratio=record.dv_ratio,
                dv_ttm=record.dv_ttm,
                total_share=record.total_share,
                float_share=record.float_share,
                free_share=record.free_share,
                total_mv=record.total_mv,
                circ_mv=record.circ_mv,
            )
        )
    for record in bundle.security_state:
        scale = float(scales.get(record.ts_code, 1.0))
        states.append(
            SecurityStateRecord(
                ts_code=record.ts_code,
                trade_date=record.trade_date,
                is_suspended=record.is_suspended,
                is_limit_up=record.is_limit_up,
                is_limit_down=record.is_limit_down,
                prev_close=_scaled(record.prev_close, scale),
                up_limit=_scaled(record.up_limit, scale),
                down_limit=_scaled(record.down_limit, scale),
            )
        )
    for record in bundle.forward_labels:
        scale = float(scales.get(record.ts_code, 1.0))
        labels.append(
            ForwardLabelRecord(
                ts_code=record.ts_code,
                decision_date=record.decision_date,
                next_trade_date=record.next_trade_date,
                next_open=record.next_open * scale,
                next_close=record.next_close * scale,
                forward_return=record.forward_return,
                label_ready=record.label_ready,
            )
        )
    return MarketBundle(
        daily_bars=tuple(bars),
        trade_calendar=bundle.trade_calendar,
        security_state=tuple(states),
        forward_labels=tuple(labels),
        daily_basic_metrics=tuple(basics),
        stock_metadata=bundle.stock_metadata,
        adjustment_factors=bundle.adjustment_factors,
    )


def _rebase_current_prices(
    store: RevisionStore,
    scales: Mapping[str, float],
    *,
    status: RevisionStatus | str,
    observed_at_utc: str,
) -> dict[str, Any]:
    corrections = {
        code: 1.0 / float(scale)
        for code, scale in scales.items()
        if float(scale) > 0 and abs(float(scale) - 1.0) > 1e-9
    }
    if not corrections:
        return {}

    daily_bars: list[DailyBarRecord] = []
    for row in store.current_records("daily_bars"):
        payload = _record_payload(row)
        correction = corrections.get(str(payload.get("ts_code") or ""))
        if correction is None:
            continue
        record = DailyBarRecord.from_dict(payload)
        daily_bars.append(
            replace(
                record,
                open=record.open * correction,
                high=record.high * correction,
                low=record.low * correction,
                close=record.close * correction,
                pre_close=_scaled(record.pre_close, correction),
                change=_scaled(record.change, correction),
            )
        )

    daily_basic: list[DailyBasicMetricRecord] = []
    for row in store.current_records("daily_basic_metrics"):
        payload = _record_payload(row)
        correction = corrections.get(str(payload.get("ts_code") or ""))
        if correction is None:
            continue
        record = DailyBasicMetricRecord.from_dict(payload)
        daily_basic.append(
            replace(record, close=_scaled(record.close, correction))
        )

    security_states: list[SecurityStateRecord] = []
    for row in store.current_records("security_state"):
        payload = _record_payload(row)
        correction = corrections.get(str(payload.get("ts_code") or ""))
        if correction is None:
            continue
        record = SecurityStateRecord.from_dict(payload)
        security_states.append(
            replace(
                record,
                prev_close=_scaled(record.prev_close, correction),
                up_limit=_scaled(record.up_limit, correction),
                down_limit=_scaled(record.down_limit, correction),
            )
        )

    forward_labels: list[ForwardLabelRecord] = []
    for row in store.current_records("forward_labels"):
        payload = _record_payload(row)
        correction = corrections.get(str(payload.get("ts_code") or ""))
        if correction is None:
            continue
        record = ForwardLabelRecord.from_dict(payload)
        forward_labels.append(
            replace(
                record,
                next_open=record.next_open * correction,
                next_close=record.next_close * correction,
            )
        )

    datasets = (
        ("daily_bars", daily_bars),
        ("daily_basic_metrics", daily_basic),
        ("security_state", security_states),
        ("forward_labels", forward_labels),
    )
    rebased: dict[str, Any] = {}
    for dataset, records in datasets:
        result = ingest_records(
            store,
            dataset,
            records,
            status=status,
            source="adjustment_rebase",
            observed_at_utc=observed_at_utc,
            bulk=True,
            run_metadata={"operation": "adjustment_rebase"},
        )
        rebased[dataset] = result.to_dict()
    return {
        "corrections_by_code": corrections,
        "datasets": rebased,
    }


def _write_parity_report(report: Mapping[str, Any], output_path: str | Path) -> Path:
    target = Path(output_path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)
    return target


def _adjustment_basis_dates(bundle: MarketBundle) -> dict[str, str]:
    dates: dict[str, str] = {}
    for factor in bundle.adjustment_factors:
        if factor.adjustment_basis_date is None:
            continue
        current = dates.get(factor.ts_code)
        if current is None or factor.adjustment_basis_date > current:
            dates[factor.ts_code] = factor.adjustment_basis_date
    return dates


def sync_market(
    store: RevisionStore | str | Path,
    provider: Any,
    ts_codes: Sequence[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    count: int = 520,
    status: RevisionStatus | str = RevisionStatus.OBSERVED,
    parity_output: str | Path | None = None,
    observed_at_utc: str | None = None,
    skip_existing_check: bool = False,
    include_daily_basic: bool = True,
    rebase_price_basis: bool = False,
    latest_only: bool = False,
) -> dict[str, Any]:
    """Fetch real bars once, verify overlap, then ingest V3 market datasets."""
    resolved_store = store if isinstance(store, RevisionStore) else RevisionStore(store)
    codes = sorted({str(code).strip() for code in ts_codes if str(code).strip()})
    if not codes:
        raise MarketSyncError("at least one ts_code is required")
    source_name = str(getattr(provider, "source_name", "") or "").strip()
    raw_price_basis = str(getattr(provider, "price_basis", "")).strip().lower() == "raw"
    fetch_bundle = getattr(provider, "fetch_history_bundle", None)
    if not source_name or not callable(fetch_bundle):
        raise MarketSyncError("provider must expose source_name and fetch_history_bundle")

    checked_at = observed_at_utc or _utc_now()
    bundle = fetch_bundle(
        codes,
        start_date=start_date,
        end_date=end_date,
        count=count,
    )
    if latest_only:
        scales: dict[str, float] = {}
        volume_scales: dict[str, float] = {}
        rebased: dict[str, Any] = {}
        if not raw_price_basis:
            indexed_daily = _current_rows_by_keys(
                resolved_store,
                "daily_bars",
                [record.natural_key() for record in bundle.daily_bars],
            )
            primary_daily = {
                _daily_key(_record_payload(row)): _record_payload(row)
                for row in indexed_daily.values()
            }
            scales = _price_scales_from_primary(bundle, primary_daily)
            volume_scales = _volume_scales_from_primary(bundle, primary_daily)
            bundle = _normalize_bundle(bundle, scales, volume_scales)
        parity = {
            "artifact_type": "kinace_v3_source_parity",
            "schema_version": SCHEMA_VERSION,
            "primary_source": "current_v3_revisions",
            "incoming_source": source_name,
            "checked_at_utc": checked_at,
            "overlap_records": 0,
            "mismatch_records": 0,
            "mismatch_ratio": 0.0,
            "volume_mismatch_records": 0,
            "volume_mismatch_ratio": 0.0,
            "mismatches": [],
            "max_delta": {},
            "comparison_policy": {
                "absolute_prices_compared": False,
                "price_metrics": [
                    "close_return",
                    "open_to_close",
                    "high_to_close",
                    "low_to_close",
                ],
                "volume_normalized_to_shares": True,
            },
            "passed": True,
            "status": "skipped",
            "acceptance_policy": {
                "max_total_mismatch_ratio": MAX_TOTAL_MISMATCH_RATIO,
                "max_volume_mismatch_ratio": MAX_VOLUME_MISMATCH_RATIO,
                "max_close_return_delta": MAX_CLOSE_RETURN_DELTA,
                "max_shape_delta": MAX_SHAPE_DELTA,
            },
            "policy": {
                "model_outputs_used_as_labels": False,
                "provisional_records_eligible": False,
                "release_requires_real_complete_records": True,
            },
        }
        parity_path = None
    else:
        scales: dict[str, float] = {}
        volume_scales: dict[str, float] = {}
        rebased = {}
        scales = _price_scales(resolved_store, bundle)
        volume_scales = _volume_scales(resolved_store, bundle)
        if rebase_price_basis and scales:
            rebased = _rebase_current_prices(
                resolved_store,
                scales,
                status=status,
                observed_at_utc=checked_at,
            )
            scales = {}
            volume_scales = {}
        elif not raw_price_basis:
            bundle = _normalize_bundle(bundle, scales, volume_scales)
        parity = _parity_report(
            resolved_store,
            bundle,
            incoming_source=source_name,
            checked_at_utc=checked_at,
        )
        parity_path = (
            Path(parity_output).resolve(strict=False)
            if parity_output is not None
            else None
        )
        if parity_path is not None:
            _write_parity_report(parity, parity_path)
        if not parity["passed"]:
            raise MarketSyncError(
                f"source parity failed for {parity['mismatch_records']} overlapping records"
            )

    ingested: dict[str, Any] = {}
    datasets = (
        ("trade_calendar", bundle.trade_calendar),
        ("daily_bars", bundle.daily_bars),
        ("adjustment_factors", bundle.adjustment_factors),
        ("security_state", bundle.security_state),
        ("forward_labels", bundle.forward_labels),
    )
    if include_daily_basic:
        datasets = (
            datasets[0],
            datasets[1],
            datasets[2],
            ("daily_basic_metrics", bundle.daily_basic_metrics),
            datasets[3],
            datasets[4],
        )
    for dataset, records in datasets:
        if latest_only:
            missing_records = _missing_indexed_records(
                resolved_store,
                dataset,
                records,
            )
        elif skip_existing_check:
            missing_records = list(records)
        else:
            missing_records = _missing_records(resolved_store, dataset, records)
        result = ingest_records(
            resolved_store,
            dataset,
            missing_records,
            status=status,
            source=source_name,
            observed_at_utc=checked_at,
            bulk=True,
            run_metadata={
                "operation": "market_sync",
                "provider": source_name,
                "source_parity_passed": True,
            },
        )
        ingested[dataset] = result.to_dict()

    return {
        "artifact_type": "kinace_v3_market_sync",
        "schema_version": SCHEMA_VERSION,
        "database": str(resolved_store.database),
        "provider": source_name,
        "codes": codes,
        "date_range": {"start": start_date, "end": end_date},
        "parity": parity,
        "price_scale_by_code": scales,
        "volume_scale_by_code": volume_scales,
        "adjustment_basis_dates_by_code": _adjustment_basis_dates(bundle),
        "rebased": rebased,
        "price_basis": "raw" if raw_price_basis else "adjusted",
        "latest_only": latest_only,
        "parity_output": str(parity_path) if parity_path else None,
        "ingested": ingested,
        "observed_at_utc": checked_at,
    }


def sync_tencent_market(
    store: RevisionStore | str | Path,
    provider: TencentMarketProvider,
    ts_codes: Sequence[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """Backward-compatible Tencent-only sync wrapper."""
    if not isinstance(provider, TencentMarketProvider):
        raise MarketSyncError("provider must be TencentMarketProvider")
    return sync_market(store, provider, ts_codes, **kwargs)


__all__ = ["MarketSyncError", "sync_market", "sync_tencent_market"]
