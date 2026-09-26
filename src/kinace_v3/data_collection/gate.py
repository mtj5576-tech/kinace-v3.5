"""Development and release gates for current data-collection revisions."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from ..config import PROJECT_ROOT, database_path, load_config, validate_database_path
from .models import RevisionStatus
from .quality import (
    DEVELOPMENT_MIN_DECISION_DATES,
    RELEASE_MIN_DECISION_DATES,
    audit_records,
    evaluate_quality_eligibility,
)
from .store import RevisionStore


GATE_SCHEMA_VERSION = 1
DEVELOPMENT_MODE = "development"
RELEASE_MODE = "release"
RELEASE_MIN_NONZERO_CANDIDATE_TRADES = 30
DEVELOPMENT_MIN_NONZERO_CANDIDATE_TRADES = 0
RELEASE_MIN_INDEPENDENT_TRADE_DATES = 20
REAL_STATUSES = frozenset(
    {RevisionStatus.OBSERVED.value, RevisionStatus.BACKFILLED_RELIABLE.value}
)
PROVISIONAL_STATUS = RevisionStatus.PROVISIONAL.value
DEFAULT_REQUIRED_POINT_IN_TIME_FIELDS = ("decision_date", "observed_at_utc")

DEFAULT_DEVELOPMENT_OUTPUT_PATH = (
    PROJECT_ROOT / "outputs" / "data_gate.development.json"
)
DEFAULT_RELEASE_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "data_gate.release.json"
DEFAULT_DEVELOPMENT_GATE_PATH = DEFAULT_DEVELOPMENT_OUTPUT_PATH
DEFAULT_RELEASE_GATE_PATH = DEFAULT_RELEASE_OUTPUT_PATH


class DataGateError(ValueError):
    """Raised when a collection-gate request is malformed."""


@dataclass(frozen=True)
class GateThresholds:
    """Thresholds that distinguish pipeline-validation and release gates."""

    release_min_decision_dates: int = RELEASE_MIN_DECISION_DATES
    development_min_decision_dates: int = DEVELOPMENT_MIN_DECISION_DATES
    release_min_nonzero_candidate_trades: int = (
        RELEASE_MIN_NONZERO_CANDIDATE_TRADES
    )
    development_min_nonzero_candidate_trades: int = (
        DEVELOPMENT_MIN_NONZERO_CANDIDATE_TRADES
    )
    release_min_independent_trade_dates: int = RELEASE_MIN_INDEPENDENT_TRADE_DATES


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_mode(mode: str) -> str:
    normalized = str(mode).strip().lower()
    normalized = {"dev": DEVELOPMENT_MODE, "prod": RELEASE_MODE}.get(
        normalized, normalized
    )
    if normalized not in {DEVELOPMENT_MODE, RELEASE_MODE}:
        raise DataGateError("mode must be development or release")
    return normalized


def _read_only_uri(path: Path) -> str:
    normalized = path.resolve(strict=False).as_posix()
    return f"file:{quote(normalized, safe='/:')}?mode=ro"


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }


def _current_rows_from_database(
    database: str | os.PathLike[str],
    *,
    datasets: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    target = Path(database).resolve(strict=False)
    if not target.is_file():
        return []
    connection = sqlite3.connect(_read_only_uri(target), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if "current_revisions" not in _table_names(connection):
            return []
        sql = """
            SELECT * FROM current_revisions
        """
        params: tuple[Any, ...] = ()
        if datasets:
            placeholders = ", ".join("?" for _ in datasets)
            sql += f" WHERE dataset IN ({placeholders})"
            params = tuple(datasets)
        sql += " ORDER BY dataset, natural_key_json, revision_sequence"
        rows = connection.execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["natural_key"] = _json_value(item.pop("natural_key_json", {}))
            item["record"] = _json_value(item.pop("record_json", {}))
            item["payload"] = item["record"]
            result.append(item)
        return result
    finally:
        connection.close()


def _current_rows(
    database_or_store: RevisionStore | str | os.PathLike[str] | None,
    *,
    datasets: Sequence[str] | None = None,
) -> tuple[Path | None, list[dict[str, Any]]]:
    if isinstance(database_or_store, RevisionStore):
        return database_or_store.database, database_or_store.current_records()
    target = (
        database_path(load_config())
        if database_or_store is None
        else validate_database_path(database_or_store)
    )
    return target, _current_rows_from_database(target, datasets=datasets)


def _decision_date(row: Mapping[str, Any]) -> str | None:
    record = row.get("record")
    record = record if isinstance(record, Mapping) else {}
    for source in (record, row, row.get("natural_key")):
        if not isinstance(source, Mapping):
            continue
        for field in ("decision_date", "trade_date", "cal_date", "start_date"):
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _date_set(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        value
        for row in rows
        if (value := _decision_date(row)) is not None
    }


def _dataset(row: Mapping[str, Any]) -> str:
    return str(row.get("dataset") or "")


def _record_mapping(row: Mapping[str, Any]) -> Mapping[str, Any]:
    record = row.get("record")
    return record if isinstance(record, Mapping) else row


def _context_key(
    record: Mapping[str, Any], date_field: str
) -> tuple[str, str] | None:
    code = record.get("ts_code") or record.get("code")
    trade_date = record.get(date_field) or record.get("date")
    if code is None or trade_date is None:
        return None
    return str(code), str(trade_date)


def _quality_inputs_for(
    row: Mapping[str, Any],
    *,
    decision_date: str | None,
    labels: Mapping[str, Any] | None,
    quality_inputs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record = row.get("record")
    record = dict(record) if isinstance(record, Mapping) else {}
    natural_key = row.get("natural_key")
    natural_key = dict(natural_key) if isinstance(natural_key, Mapping) else {}
    result: dict[str, Any] = {**row, **record}
    result["decision_date"] = decision_date or _decision_date(row)
    result["observed_at_utc"] = row.get("observed_at_utc")
    result["recorded_at_utc"] = row.get("recorded_at_utc")

    additions: dict[str, Any] = {}
    for key in (decision_date, natural_key.get("trade_date"), natural_key.get("cal_date")):
        if key is not None and isinstance(quality_inputs, Mapping):
            value = quality_inputs.get(str(key))
            if isinstance(value, Mapping):
                additions.update(value)
        if key is not None and isinstance(labels, Mapping):
            value = labels.get(str(key))
            if isinstance(value, Mapping):
                additions["labels"] = dict(value)
    if isinstance(quality_inputs, Mapping):
        direct = quality_inputs.get("default")
        if isinstance(direct, Mapping):
            additions.update(direct)
    if isinstance(labels, Mapping) and any(
        name in labels for name in ("forward_return", "return", "label")
    ):
        additions["labels"] = dict(labels)
    result.update(additions)
    return result


def _load_labels(path: str | os.PathLike[str] | None) -> Any:
    if path is None:
        return None
    target = Path(path).resolve(strict=False)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise DataGateError(f"cannot read labels artifact: {target}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        result: list[Any] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DataGateError(
                    f"labels artifact is neither JSON nor JSONL: {target}"
                ) from exc
        return result


def _as_lookup(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            key = item.get("decision_date") or item.get("trade_date") or item.get("date")
            if key is not None:
                result[str(key)] = dict(item)
        return result
    return None


def _merge_coverage(
    target: dict[str, dict[str, Any]],
    source: Mapping[str, Any],
) -> None:
    for field, raw in source.items():
        if not isinstance(raw, Mapping):
            continue
        entry = target.setdefault(field, {"present": 0, "total": 0, "missing": 0})
        entry["present"] += int(raw.get("present") or 0)
        entry["total"] += int(raw.get("total") or 0)
        entry["missing"] += int(raw.get("missing") or 0)
        total = entry["total"]
        ratio = entry["present"] / total if total else 0.0
        entry["ratio"] = round(ratio, 6)
        entry["percent"] = round(ratio * 100.0, 4)


def _merge_issues(reports: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for report in reports:
        for raw in report.get("issues") or []:
            if not isinstance(raw, Mapping):
                continue
            code = str(raw.get("code") or "unknown")
            item = merged.setdefault(
                code,
                {
                    "code": code,
                    "severity": str(raw.get("severity") or "error"),
                    "message": str(raw.get("message") or ""),
                    "count": 0,
                },
            )
            item["count"] += int(raw.get("count") or 1)
            details = raw.get("details")
            if details is not None:
                existing = item.setdefault("details", [])
                if isinstance(details, Sequence) and not isinstance(details, (str, bytes)):
                    existing.extend(details)
                else:
                    existing.append(details)
                item["details"] = existing[:100]
    return list(merged.values())


def _aggregate_quality_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
    required_point_in_time_fields: Sequence[str],
) -> dict[str, Any]:
    if not reports:
        raise DataGateError("quality report batches must not be empty")
    required_coverage: dict[str, dict[str, Any]] = {}
    field_coverage: dict[str, dict[str, Any]] = {}
    missing_values: defaultdict[str, int] = defaultdict(int)
    state_coverage: dict[str, dict[str, Any]] = {}
    label_coverage: dict[str, dict[str, Any]] = {}
    status_counts: defaultdict[str, int] = defaultdict(int)
    missing_required: list[Any] = []
    duplicate_details: list[Any] = []
    date_violations: list[Any] = []
    invalid_dates: list[Any] = []
    ohlc_violations: list[Any] = []
    state_invalid_values: list[Any] = []
    label_missing_rows: list[Any] = []
    pit_missing_fields: list[Any] = []
    future_information: list[Any] = []
    decision_dates: set[str] = set()
    totals: defaultdict[str, int] = defaultdict(int)

    for report in reports:
        totals["record_count"] += int(report.get("record_count") or 0)
        totals["unique_key_count"] += int(report.get("unique_key_count") or 0)
        decision_dates.update(str(value) for value in report.get("decision_dates") or [])
        _merge_coverage(required_coverage, report.get("required_field_coverage") or {})
        _merge_coverage(field_coverage, report.get("field_coverage") or {})
        for field, value in (report.get("missing_values") or {}).items():
            missing_values[str(field)] += int(value or 0)
        duplicates = report.get("duplicate_keys") or {}
        totals["duplicate_count"] += int(duplicates.get("duplicate_count") or 0)
        totals["duplicate_key_count"] += int(duplicates.get("duplicate_key_count") or 0)
        duplicate_details.extend(duplicates.get("duplicates") or [])
        dates = report.get("date_ordering") or {}
        totals["date_violation_count"] += int(dates.get("violation_count") or 0)
        totals["invalid_date_count"] += int(dates.get("invalid_date_count") or 0)
        date_violations.extend(dates.get("violations") or [])
        invalid_dates.extend(dates.get("invalid_dates") or [])
        ohlc = report.get("invalid_ohlc_relationships") or {}
        totals["invalid_ohlc_count"] += int(ohlc.get("invalid_count") or 0)
        ohlc_violations.extend(ohlc.get("violations") or [])
        states = report.get("suspended_limit_state_inputs") or {}
        _merge_coverage(state_coverage, states.get("coverage") or {})
        totals["state_missing_count"] += int(states.get("missing_count") or 0)
        totals["state_invalid_count"] += int(states.get("invalid_count") or 0)
        totals["inferred_suspended_count"] += int(states.get("inferred_suspended_count") or 0)
        state_invalid_values.extend(states.get("invalid_values") or [])
        labels = report.get("label_readiness") or {}
        _merge_coverage(label_coverage, labels.get("coverage") or {})
        totals["label_ready_count"] += int(labels.get("ready_count") or 0)
        totals["label_missing_count"] += int(labels.get("missing_count") or 0)
        label_missing_rows.extend(labels.get("missing_rows") or [])
        pit = report.get("point_in_time_eligibility") or {}
        for status, count in (pit.get("status_counts") or {}).items():
            status_counts[str(status)] += int(count or 0)
        for key in (
            "eligible_count",
            "point_in_time_eligible_count",
            "noneligible_count",
            "provisional_count",
            "real_count",
            "missing_count",
            "future_information_count",
        ):
            totals[key] += int(pit.get(key) or 0)
        pit_missing_fields.extend(pit.get("missing_fields") or [])
        future_information.extend(pit.get("future_information") or [])
        missing_required.extend(report.get("missing_required") or [])

    required_coverage_ratio = (
        min(float(item["ratio"]) for item in required_coverage.values())
        if required_coverage
        else 0.0
    )
    pit_eligible = bool(
        totals["eligible_count"] == totals["record_count"]
        and totals["future_information_count"] == 0
        and totals["missing_count"] == 0
    )
    state_ready = bool(
        totals["state_missing_count"] == 0 and totals["state_invalid_count"] == 0
    )
    labels_ready = totals["label_ready_count"] > 0
    passed = bool(
        all(bool(report.get("passed")) for report in reports)
        and totals["duplicate_count"] == 0
        and totals["date_violation_count"] == 0
        and totals["invalid_date_count"] == 0
        and totals["invalid_ohlc_count"] == 0
    )
    result: dict[str, Any] = {
        "artifact_type": "kinace_v3_data_quality",
        "schema_version": reports[0].get("schema_version"),
        "dataset": "daily_bars",
        "status": "passed" if passed else "failed",
        "passed": passed,
        "valid": passed,
        "record_count": totals["record_count"],
        "unique_key_count": totals["unique_key_count"],
        "decision_date_count": len(decision_dates),
        "decision_dates": sorted(decision_dates),
        "required_fields": list(required_point_in_time_fields),
        "required_field_coverage": required_coverage,
        "required_field_coverage_ratio": required_coverage_ratio,
        "field_coverage": field_coverage,
        "missing_values": dict(sorted(missing_values.items())),
        "missing_value_count": sum(missing_values.values()),
        "missing_required": missing_required[:100],
        "date_ordering": {
            "valid": totals["date_violation_count"] == 0
            and totals["invalid_date_count"] == 0,
            "violation_count": totals["date_violation_count"],
            "invalid_date_count": totals["invalid_date_count"],
            "violations": date_violations[:100],
            "invalid_dates": invalid_dates[:100],
        },
        "date_order_valid": totals["date_violation_count"] == 0
        and totals["invalid_date_count"] == 0,
        "invalid_ohlc_relationships": {
            "valid": totals["invalid_ohlc_count"] == 0,
            "invalid_count": totals["invalid_ohlc_count"],
            "violations": ohlc_violations[:100],
        },
        "invalid_ohlc_count": totals["invalid_ohlc_count"],
        "duplicate_keys": {
            "valid": totals["duplicate_count"] == 0,
            "duplicate_count": totals["duplicate_count"],
            "duplicate_key_count": totals["duplicate_key_count"],
            "unique_key_count": totals["unique_key_count"],
            "duplicates": duplicate_details[:100],
            "missing_key_rows": [],
        },
        "duplicate_key_count": totals["duplicate_count"],
        "suspended_limit_state_inputs": {
            "valid": state_ready,
            "state_inputs_ready": state_ready,
            "provided_fields": sorted(state_coverage),
            "coverage": state_coverage,
            "missing_count": totals["state_missing_count"],
            "invalid_count": totals["state_invalid_count"],
            "invalid_values": state_invalid_values[:100],
            "inferred_suspended_count": totals["inferred_suspended_count"],
        },
        "state_inputs_ready": state_ready,
        "label_readiness": {
            "ready": labels_ready,
            "fully_ready": totals["label_ready_count"] == totals["record_count"],
            "ready_count": totals["label_ready_count"],
            "total": totals["record_count"],
            "missing_count": totals["label_missing_count"],
            "missing_rows": label_missing_rows[:100],
            "label_fields": reports[0].get("label_readiness", {}).get("label_fields", []),
            "coverage": label_coverage,
        },
        "labels_ready": labels_ready,
        "point_in_time_eligibility": {
            "eligible": pit_eligible,
            "point_in_time_eligible": pit_eligible,
            "eligible_count": totals["eligible_count"],
            "point_in_time_eligible_count": totals["point_in_time_eligible_count"],
            "noneligible_count": totals["noneligible_count"],
            "status_counts": dict(sorted(status_counts.items())),
            "provisional_count": totals["provisional_count"],
            "real_count": totals["real_count"],
            "missing_count": totals["missing_count"],
            "missing_fields": pit_missing_fields[:100],
            "future_information_count": totals["future_information_count"],
            "future_information": future_information[:100],
            "required_fields": list(required_point_in_time_fields),
        },
        "point_in_time_eligible": pit_eligible,
        "as_of_utc": as_of_utc,
    }
    result["issues"] = _merge_issues(reports)
    result["failures"] = result["issues"]
    result["eligibility"] = {
        "development": evaluate_quality_eligibility(result, mode="development"),
        "release": evaluate_quality_eligibility(result, mode="release"),
    }
    result["development_eligible"] = result["eligibility"]["development"][
        "development_eligible"
    ]
    result["release_eligible"] = result["eligibility"]["release"]["release_eligible"]
    result["production_eligible"] = result["release_eligible"]
    result["training_eligible"] = result["release_eligible"]
    return result


def _iter_gate_dataset_rows(database: str | os.PathLike[str], dataset: str) -> Iterable[dict[str, Any]]:
    target = Path(database).resolve(strict=False)
    if not target.is_file():
        return
    connection = sqlite3.connect(_read_only_uri(target), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            """
            SELECT r.*
            FROM data_revisions r
            WHERE r.dataset = ?
              AND r.status <> 'superseded'
              AND NOT EXISTS (
                  SELECT 1 FROM data_revisions child
                  WHERE child.supersedes_revision_id = r.revision_id
                    AND child.status <> 'superseded'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM data_revisions newer
                  WHERE newer.dataset = r.dataset
                    AND newer.natural_key_json = r.natural_key_json
                    AND newer.revision_sequence > r.revision_sequence
                    AND newer.status <> 'superseded'
              )
            ORDER BY r.natural_key_json
            """,
            (dataset,),
        )
        for row in rows:
            item = dict(row)
            item["natural_key"] = _json_value(item.pop("natural_key_json", {}))
            item["record"] = _json_value(item.pop("record_json", {}))
            item["payload"] = item["record"]
            yield item
    finally:
        connection.close()


def _audit_quality_rows_grouped(
    selected_rows: Iterable[Mapping[str, Any]],
    *,
    state_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    label_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    labels: Mapping[str, Any] | None,
    quality_inputs: Mapping[str, Any] | None,
    as_of_utc: str,
    required_point_in_time_fields: Sequence[str],
    require_state_inputs: bool,
    require_point_in_time: bool,
) -> dict[str, Any]:
    ordered = selected_rows
    reports: list[Mapping[str, Any]] = []
    batch: list[dict[str, Any]] = []
    batch_date: str | None = None
    for row in ordered:
        merged = _quality_inputs_for(
            row,
            decision_date=_decision_date(row),
            labels=labels,
            quality_inputs=quality_inputs,
        )
        code = merged.get("ts_code") or merged.get("code")
        decision_date = merged.get("decision_date")
        if code is not None and decision_date is not None:
            state = state_lookup.get((str(code), str(decision_date)))
            if state:
                merged.update(state)
            label = label_lookup.get((str(code), str(decision_date)))
            if label:
                merged["labels"] = label
        current_date = str(decision_date) if decision_date is not None else None
        if batch and current_date != batch_date:
            reports.append(
                audit_records(
                    "daily_bars",
                    batch,
                    as_of_utc=as_of_utc,
                    point_in_time_fields=tuple(required_point_in_time_fields),
                    require_state_inputs=require_state_inputs,
                    require_point_in_time=require_point_in_time,
                )
            )
            batch = []
        batch_date = current_date
        batch.append(merged)
    if batch:
        reports.append(
            audit_records(
                "daily_bars",
                batch,
                as_of_utc=as_of_utc,
                point_in_time_fields=tuple(required_point_in_time_fields),
                require_state_inputs=require_state_inputs,
                require_point_in_time=require_point_in_time,
            )
        )
    if not reports:
        reports.append(
            audit_records(
                "daily_bars",
                [],
                as_of_utc=as_of_utc,
                point_in_time_fields=tuple(required_point_in_time_fields),
                require_state_inputs=require_state_inputs,
                require_point_in_time=require_point_in_time,
            )
        )
    return _aggregate_quality_reports(
        reports,
        as_of_utc=as_of_utc,
        required_point_in_time_fields=required_point_in_time_fields,
    )


def _stream_release_gate_inputs(
    database: str | os.PathLike[str],
    *,
    labels: Mapping[str, Any] | None,
    quality_inputs: Mapping[str, Any] | None,
    as_of_utc: str,
    required_point_in_time_fields: Sequence[str],
) -> dict[str, Any]:
    target = Path(database).resolve(strict=False)
    state_lookup: dict[tuple[str, str], Mapping[str, Any]] = {}
    label_lookup: dict[tuple[str, str], Mapping[str, Any]] = {}
    state_count = 0
    label_count = 0
    for row in _iter_gate_dataset_rows(target, "security_state"):
        state_count += 1
        key = _context_key(_record_mapping(row), "trade_date")
        if key is not None:
            state_lookup[key] = _record_mapping(row)
    for row in _iter_gate_dataset_rows(target, "forward_labels"):
        label_count += 1
        key = _context_key(_record_mapping(row), "decision_date")
        if key is not None:
            label_lookup[key] = _record_mapping(row)

    daily_count = 0
    approved_count = 0
    provisional_count = 0
    approved_dates: set[str] = set()
    provisional_dates: set[str] = set()

    def _selected_rows() -> Iterable[Mapping[str, Any]]:
        nonlocal daily_count, approved_count, provisional_count
        for row in _iter_gate_dataset_rows(target, "daily_bars"):
            daily_count += 1
            status = str(row.get("effective_status") or row.get("status"))
            decision_date = _decision_date(row)
            if status in REAL_STATUSES:
                approved_count += 1
                if decision_date is not None:
                    approved_dates.add(decision_date)
                yield row
            elif status == PROVISIONAL_STATUS:
                provisional_count += 1
                if decision_date is not None:
                    provisional_dates.add(decision_date)
                yield row

    quality_report = _audit_quality_rows_grouped(
        _selected_rows(),
        state_lookup=state_lookup,
        label_lookup=label_lookup,
        labels=labels,
        quality_inputs=quality_inputs,
        as_of_utc=as_of_utc,
        required_point_in_time_fields=required_point_in_time_fields,
        require_state_inputs=True,
        require_point_in_time=True,
    )
    connection = sqlite3.connect(_read_only_uri(target), uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        current_record_count = int(
            connection.execute("SELECT COUNT(*) FROM current_revisions").fetchone()[0]
        )
    finally:
        connection.close()
    return {
        "database": target,
        "current_record_count": current_record_count,
        "daily_bar_records": daily_count,
        "security_state_records": state_count,
        "forward_label_records": label_count,
        "data_approved_records": approved_count,
        "provisional_records": provisional_count,
        "approved_dates": approved_dates,
        "provisional_dates": provisional_dates,
        "quality_report": quality_report,
    }


def count_nonzero_candidate_specific_trades(
    database: str | os.PathLike[str] | None,
) -> int:
    """Count nonzero V3 trial trades linked to an explicit candidate and run."""
    if database is None:
        return 0
    target = Path(database).resolve(strict=False)
    if not target.is_file():
        return 0
    connection = sqlite3.connect(_read_only_uri(target), uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = _table_names(connection)
        if "backtest_trades" not in tables or "backtest_runs" not in tables:
            return 0
        trade_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(backtest_trades)").fetchall()
        }
        run_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(backtest_runs)").fetchall()
        }
        if not {"run_id", "candidate_id", "shares"}.issubset(trade_columns):
            return 0
        if not {"run_id", "candidate_id"}.issubset(run_columns):
            return 0
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM backtest_trades AS trade
            JOIN backtest_runs AS run ON run.run_id = trade.run_id
            WHERE trade.shares IS NOT NULL
              AND trade.shares <> 0
              AND trade.candidate_id IS NOT NULL
              AND trade.candidate_id <> ''
              AND trade.candidate_id = run.candidate_id
            """
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        connection.close()


def candidate_evidence_summary(
    database: str | os.PathLike[str] | None,
    *,
    loop_id: str | None = None,
    candidate_signature: str | None = None,
) -> dict[str, int | bool]:
    """Count candidate evidence bound to a passing same-cycle audit."""
    empty = {
        "bound_trades": 0,
        "independent_trade_dates": 0,
        "distinct_candidates": 0,
        "distinct_audits": 0,
        "bound": False,
    }
    if database is None:
        return empty
    target = Path(database).resolve(strict=False)
    if not target.is_file():
        return empty
    connection = sqlite3.connect(_read_only_uri(target), uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = _table_names(connection)
        required_tables = {
            "backtest_trades",
            "backtest_runs",
            "research_audits",
            "research_loop_runs",
            "research_evidence_runs",
        }
        if not required_tables.issubset(tables):
            return empty
        trade_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(backtest_trades)").fetchall()
        }
        audit_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(research_audits)").fetchall()
        }
        if not {"run_id", "candidate_id", "code", "decision_date", "shares"}.issubset(
            trade_columns
        ):
            return empty
        if not {
            "audit_id",
            "status",
            "holdout_run_id",
            "candidate_audit_passed",
        }.issubset(audit_columns):
            return empty
        clauses = [
            "audit.status = 'passed'",
            "audit.candidate_audit_passed = 1",
            "loop.holdout_status = 'challenger_ready'",
            "trade.shares IS NOT NULL",
            "trade.shares <> 0",
            "trade.candidate_id = run.candidate_id",
        ]
        params: list[Any] = []
        if loop_id is not None:
            clauses.append("audit.loop_id = ?")
            params.append(str(loop_id))
        if candidate_signature is not None:
            clauses.append("audit.candidate_signature = ?")
            params.append(str(candidate_signature))
        row = connection.execute(
            f"""
            SELECT
                COUNT(DISTINCT trade.candidate_id || '|' || trade.code || '|' || trade.decision_date)
                    AS bound_trades,
                COUNT(DISTINCT trade.decision_date) AS independent_trade_dates,
                COUNT(DISTINCT audit.candidate_signature) AS distinct_candidates,
                COUNT(DISTINCT audit.audit_id) AS distinct_audits
            FROM backtest_trades AS trade
            JOIN backtest_runs AS run ON run.run_id = trade.run_id
            JOIN research_evidence_runs AS evidence_run
              ON evidence_run.run_id = run.run_id
            JOIN research_audits AS audit
              ON audit.loop_id = evidence_run.loop_id
             AND audit.candidate_signature = evidence_run.candidate_signature
            JOIN research_loop_runs AS loop ON loop.loop_id = audit.loop_id
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchone()
        if row is None:
            return empty
        return {
            "bound_trades": int(row[0] or 0),
            "independent_trade_dates": int(row[1] or 0),
            "distinct_candidates": int(row[2] or 0),
            "distinct_audits": int(row[3] or 0),
            "bound": bool(row[3] or 0),
        }
    finally:
        connection.close()


def _gate(
    gate_id: str,
    description: str,
    actual: int | bool,
    required: int | bool,
    passed: bool,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "description": description,
        "actual": actual,
        "required": required,
        "passed": bool(passed),
    }


def evaluate_gate(
    database: RevisionStore | str | os.PathLike[str] | None = None,
    *,
    mode: str = RELEASE_MODE,
    thresholds: GateThresholds | None = None,
    max_decision_dates: int | None = None,
    labels: Mapping[str, Any] | None = None,
    labels_path: str | os.PathLike[str] | None = None,
    quality_inputs: Mapping[str, Any] | None = None,
    quality_report: Mapping[str, Any] | None = None,
    candidate_loop_id: str | None = None,
    candidate_signature: str | None = None,
    required_point_in_time_fields: Sequence[str] = (
        DEFAULT_REQUIRED_POINT_IN_TIME_FIELDS
    ),
    nonzero_candidate_specific_trades: int | None = None,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Evaluate current revisions under development or release policy."""
    resolved_mode = _normalize_mode(mode)
    resolved_thresholds = thresholds or GateThresholds()
    generated_at = as_of_utc or _utc_now()
    if resolved_mode == RELEASE_MODE and max_decision_dates is not None:
        raise DataGateError("max_decision_dates is only valid in development mode")
    if labels is None and labels_path is not None:
        labels = _as_lookup(_load_labels(labels_path))
    streaming_release = (
        resolved_mode == RELEASE_MODE
        and quality_report is None
        and not isinstance(database, RevisionStore)
    )
    if streaming_release:
        streamed = _stream_release_gate_inputs(
            database,
            labels=labels,
            quality_inputs=quality_inputs,
            as_of_utc=generated_at,
            required_point_in_time_fields=required_point_in_time_fields,
        )
        resolved_database = streamed["database"]
        current_record_count = int(streamed["current_record_count"])
        daily_bar_records = int(streamed["daily_bar_records"])
        security_state_records = int(streamed["security_state_records"])
        forward_label_records = int(streamed["forward_label_records"])
        data_approved_records = int(streamed["data_approved_records"])
        provisional_records = int(streamed["provisional_records"])
        approved_dates = streamed["approved_dates"]
        provisional_dates = streamed["provisional_dates"]
        quality_report = streamed["quality_report"]
    else:
        required_datasets = ("daily_bars", "security_state", "forward_labels")
        resolved_database, rows = _current_rows(database, datasets=required_datasets)
        daily_rows = [row for row in rows if _dataset(row) == "daily_bars"]
        state_lookup = {
            key: _record_mapping(row)
            for row in rows
            if _dataset(row) == "security_state"
            if (key := _context_key(_record_mapping(row), "trade_date")) is not None
        }
        label_lookup = {
            key: _record_mapping(row)
            for row in rows
            if _dataset(row) == "forward_labels"
            if (key := _context_key(_record_mapping(row), "decision_date")) is not None
        }
        real_rows = [
            row
            for row in daily_rows
            if str(row.get("effective_status") or row.get("status")) in REAL_STATUSES
        ]
        provisional_rows = [
            row
            for row in rows
            if str(row.get("effective_status") or row.get("status"))
            == PROVISIONAL_STATUS
        ]
        approved_dates = _date_set(real_rows)
        provisional_dates = _date_set(provisional_rows)

        selected_real_rows = real_rows
        selected_provisional_rows = provisional_rows
        if resolved_mode == DEVELOPMENT_MODE and max_decision_dates is not None:
            if isinstance(max_decision_dates, bool) or max_decision_dates <= 0:
                raise DataGateError("max_decision_dates must be a positive integer")
            allowed_dates = set(
                sorted(approved_dates | provisional_dates)[-max_decision_dates:]
            )
            selected_real_rows = [
                row for row in real_rows if _decision_date(row) in allowed_dates
            ]
            selected_provisional_rows = [
                row
                for row in provisional_rows
                if _decision_date(row) in allowed_dates
            ]

        selected_rows = selected_real_rows + selected_provisional_rows
        if quality_report is None:
            quality_report = _audit_quality_rows_grouped(
                selected_rows,
                state_lookup=state_lookup,
                label_lookup=label_lookup,
                labels=labels,
                quality_inputs=quality_inputs,
                as_of_utc=generated_at,
                required_point_in_time_fields=required_point_in_time_fields,
                require_state_inputs=resolved_mode == RELEASE_MODE,
                require_point_in_time=resolved_mode == RELEASE_MODE,
            )
        current_record_count = len(rows)
        daily_bar_records = len(daily_rows)
        security_state_records = len(state_lookup)
        forward_label_records = len(label_lookup)
        data_approved_records = len(real_rows)
        provisional_records = len(provisional_rows)
    quality_eligibility = evaluate_quality_eligibility(
        quality_report,
        mode=resolved_mode,
        min_decision_dates=(
            resolved_thresholds.release_min_decision_dates
            if resolved_mode == RELEASE_MODE
            else resolved_thresholds.development_min_decision_dates
        ),
        max_decision_dates=max_decision_dates if resolved_mode == DEVELOPMENT_MODE else None,
        allow_provisional=resolved_mode == DEVELOPMENT_MODE,
        require_labels=resolved_mode == RELEASE_MODE,
        require_state_inputs=resolved_mode == RELEASE_MODE,
        require_point_in_time=resolved_mode == RELEASE_MODE,
    )

    raw_nonzero_candidate_specific_trades = count_nonzero_candidate_specific_trades(
        resolved_database
    )
    if nonzero_candidate_specific_trades is not None:
        raw_nonzero_candidate_specific_trades = int(nonzero_candidate_specific_trades)
    evidence = candidate_evidence_summary(
        resolved_database,
        loop_id=candidate_loop_id,
        candidate_signature=candidate_signature,
    )
    eligible_candidate_trades = (
        int(evidence["bound_trades"])
        if resolved_mode == RELEASE_MODE
        else raw_nonzero_candidate_specific_trades
    )

    min_dates = (
        resolved_thresholds.release_min_decision_dates
        if resolved_mode == RELEASE_MODE
        else resolved_thresholds.development_min_decision_dates
    )
    min_trades = (
        resolved_thresholds.release_min_nonzero_candidate_trades
        if resolved_mode == RELEASE_MODE
        else resolved_thresholds.development_min_nonzero_candidate_trades
    )
    min_independent_dates = (
        resolved_thresholds.release_min_independent_trade_dates
        if resolved_mode == RELEASE_MODE
        else 0
    )
    approved_date_count = len(approved_dates)
    selected_decision_date_count = (
        len(approved_dates | provisional_dates)
        if resolved_mode == DEVELOPMENT_MODE
        else approved_date_count
    )
    point_in_time_quality = quality_report.get("point_in_time_eligibility") or {}
    pit_fields_ready = bool(
        float(quality_report.get("required_field_coverage_ratio", 0.0)) == 1.0
        and int(point_in_time_quality.get("missing_count", 0)) == 0
        and int(point_in_time_quality.get("future_information_count", 0)) == 0
    )
    pit_ready = (
        bool(quality_report.get("point_in_time_eligible")) and pit_fields_ready
        if resolved_mode == RELEASE_MODE
        else pit_fields_ready
    )
    labels_ready = bool(quality_report.get("labels_ready"))
    no_provisional = not provisional_dates

    hard_gates = [
        _gate(
            "current_data_records",
            "At least one current V3 data revision is available.",
            current_record_count,
            1,
            current_record_count > 0,
        ),
        _gate(
            "data_approved_decision_dates",
            "Development requires a window; release requires 372 current real decision dates.",
            selected_decision_date_count,
            min_dates,
            selected_decision_date_count >= min_dates,
        ),
        _gate(
            "required_point_in_time_fields",
            "All selected records have complete, point-in-time-eligible required fields.",
            pit_ready,
            True,
            pit_ready,
        ),
        _gate(
            "label_readiness",
            "Labels are ready for model evaluation.",
            labels_ready,
            True,
            labels_ready if resolved_mode == RELEASE_MODE else True,
        ),
        _gate(
            "no_provisional_dates",
            "Release data contains no current provisional dates.",
            len(provisional_dates),
            0,
            no_provisional if resolved_mode == RELEASE_MODE else True,
        ),
        _gate(
            "nonzero_candidate_specific_trades",
            "Current V3 evidence contains enough unique candidate evidence bound to a passing same-cycle audit.",
            eligible_candidate_trades,
            min_trades,
            eligible_candidate_trades >= min_trades,
        ),
        _gate(
            "independent_candidate_trade_dates",
            "Bound candidate evidence spans enough independent decision dates.",
            int(evidence["independent_trade_dates"]),
            min_independent_dates,
            int(evidence["independent_trade_dates"]) >= min_independent_dates,
        ),
    ]
    failed = [gate["id"] for gate in hard_gates if not gate["passed"]]
    ready = not failed
    development_only = resolved_mode == DEVELOPMENT_MODE
    production_eligible = ready and not development_only
    status = (
        "development_ready"
        if ready and development_only
        else "release_ready"
        if ready
        else "blocked_insufficient_data"
    )

    return {
        "artifact_type": "kinace_v3_data_collection_gate",
        "schema_version": GATE_SCHEMA_VERSION,
        "mode": resolved_mode,
        "status": status,
        "ready": ready,
        "development_only": development_only,
        "development_eligible": ready if development_only else False,
        "release_eligible": ready and not development_only,
        "production_eligible": production_eligible,
        "production_ready": production_eligible,
        "training_eligible": production_eligible,
        "generated_at_utc": generated_at,
        "database": str(resolved_database) if resolved_database is not None else None,
        "counts": {
            "current_records": current_record_count,
            "daily_bar_records": daily_bar_records,
            "security_state_records": security_state_records,
            "forward_label_records": forward_label_records,
            "data_approved_records": data_approved_records,
            "provisional_records": provisional_records,
            "data_approved_decision_dates": approved_date_count,
            "provisional_dates": len(provisional_dates),
            "nonzero_candidate_specific_trades": int(eligible_candidate_trades),
            "raw_nonzero_candidate_specific_trades": int(
                raw_nonzero_candidate_specific_trades
            ),
            "bound_candidate_specific_trades": int(evidence["bound_trades"]),
            "bound_independent_trade_dates": int(
                evidence["independent_trade_dates"]
            ),
            "bound_distinct_candidates": int(evidence["distinct_candidates"]),
            "bound_distinct_audits": int(evidence["distinct_audits"]),
        },
        "candidate_evidence": dict(evidence),
        "thresholds": {
            "min_decision_dates": min_dates,
            "min_nonzero_candidate_specific_trades": min_trades,
            "min_independent_trade_dates": min_independent_dates,
            "release_min_decision_dates": (
                resolved_thresholds.release_min_decision_dates
            ),
            "release_min_nonzero_candidate_specific_trades": (
                resolved_thresholds.release_min_nonzero_candidate_trades
            ),
            "release_min_independent_trade_dates": (
                resolved_thresholds.release_min_independent_trade_dates
            ),
            "max_decision_dates": (
                max_decision_dates if resolved_mode == DEVELOPMENT_MODE else None
            ),
        },
        "required_point_in_time_fields": list(required_point_in_time_fields),
        "provisional_dates": sorted(provisional_dates),
        "hard_gates": hard_gates,
        "failed_hard_gates": failed,
        "quality_eligibility": quality_eligibility,
        "quality": dict(quality_report),
    }


def write_gate(
    result: Mapping[str, Any],
    output_path: str | os.PathLike[str] | None = None,
) -> Path:
    if output_path is None:
        output_path = (
            DEFAULT_DEVELOPMENT_OUTPUT_PATH
            if _normalize_mode(str(result.get("mode") or RELEASE_MODE))
            == DEVELOPMENT_MODE
            else DEFAULT_RELEASE_OUTPUT_PATH
        )
    target = Path(output_path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)
    return target


def run_gate(
    database: RevisionStore | str | os.PathLike[str] | None = None,
    *,
    mode: str = RELEASE_MODE,
    output_path: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    resolved_mode = _normalize_mode(mode)
    target = Path(
        output_path
        or (
            DEFAULT_DEVELOPMENT_OUTPUT_PATH
            if resolved_mode == DEVELOPMENT_MODE
            else DEFAULT_RELEASE_OUTPUT_PATH
        )
    ).resolve(strict=False)
    result = evaluate_gate(database, mode=resolved_mode, **kwargs)
    result["output_path"] = str(target)
    write_gate(result, target)
    return result


evaluate_data_collection_gate = evaluate_gate
evaluate_collection_gate = evaluate_gate
build_gate = evaluate_gate
build_data_gate = evaluate_gate
run_data_collection_gate = run_gate
run_data_gate = run_gate
write_data_collection_gate = write_gate
write_collection_gate = write_gate


__all__ = [
    "DEFAULT_DEVELOPMENT_OUTPUT_PATH",
    "DEFAULT_DEVELOPMENT_GATE_PATH",
    "DEFAULT_RELEASE_OUTPUT_PATH",
    "DEFAULT_RELEASE_GATE_PATH",
    "DEFAULT_REQUIRED_POINT_IN_TIME_FIELDS",
    "DEVELOPMENT_MIN_NONZERO_CANDIDATE_TRADES",
    "DEVELOPMENT_MODE",
    "DataGateError",
    "GATE_SCHEMA_VERSION",
    "GateThresholds",
    "REAL_STATUSES",
    "RELEASE_MIN_NONZERO_CANDIDATE_TRADES",
    "RELEASE_MODE",
    "build_gate",
    "candidate_evidence_summary",
    "count_nonzero_candidate_specific_trades",
    "evaluate_data_collection_gate",
    "evaluate_collection_gate",
    "evaluate_gate",
    "build_data_gate",
    "run_data_collection_gate",
    "run_data_gate",
    "run_gate",
    "write_data_collection_gate",
    "write_collection_gate",
    "write_gate",
]
