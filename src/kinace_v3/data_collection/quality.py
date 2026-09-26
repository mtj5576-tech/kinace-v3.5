"""Quality and point-in-time audits for canonical data revisions."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields as dataclass_fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import (
    RECORD_MODELS,
    BaseRecord,
    RevisionStatus,
    normalize_dataset,
)
from .providers.base import REQUIRED_COLUMNS
from .store import RevisionStore


QUALITY_SCHEMA_VERSION = 1
RELEASE_MIN_DECISION_DATES = 372
DEVELOPMENT_MIN_DECISION_DATES = 1
LABEL_FIELDS = ("forward_return", "return", "label")
STATE_FIELDS = ("is_suspended", "is_limit_up", "is_limit_down")
STATE_ALIASES = {
    "is_suspended": ("is_suspended", "suspended"),
    "is_limit_up": ("is_limit_up", "limit_up"),
    "is_limit_down": ("is_limit_down", "limit_down"),
}
PIT_FIELDS = ("decision_date", "as_of_utc", "observed_at_utc", "recorded_at_utc")
REAL_STATUSES = frozenset(
    {RevisionStatus.OBSERVED.value, RevisionStatus.BACKFILLED_RELIABLE.value}
)


class DataQualityError(ValueError):
    """Raised when a quality-audit request or artifact is malformed."""


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _normal_date(value: Any) -> str | None:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        try:
            if len(text) >= 10 and text[4] == "-" and text[7] == "-":
                return date.fromisoformat(text[:10]).isoformat()
            digits = "".join(character for character in text if character.isdigit())
            if len(digits) >= 8:
                return date.fromisoformat(
                    f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
                ).isoformat()
        except ValueError:
            return None
    return None


def _normal_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _normal_bool(value: Any) -> bool | None:
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
    return None


def _unwrap(item: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(item, BaseRecord):
        return item.to_dict(), item.to_dict()
    if not isinstance(item, Mapping):
        raise DataQualityError("quality rows must be canonical records or mappings")
    outer = dict(item)
    record = outer.get("record")
    if isinstance(record, Mapping):
        return dict(record), outer
    payload = outer.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload), outer
    return outer, outer


def _key_for(dataset: str, record: Mapping[str, Any]) -> dict[str, Any] | None:
    model = RECORD_MODELS[dataset]
    try:
        if all(not _is_missing(record.get(name)) for name in REQUIRED_COLUMNS[dataset]):
            return model.from_dict(record).natural_key()
    except Exception:
        pass
    if dataset == "trade_calendar":
        names = ("exchange", "cal_date")
    elif dataset == "stock_metadata":
        names = ("ts_code",)
    elif dataset in {
        "daily_bars",
        "daily_basic_metrics",
        "adjustment_factors",
        "security_state",
    }:
        names = ("ts_code", "trade_date")
    elif dataset == "forward_labels":
        names = ("ts_code", "decision_date")
    elif dataset == "name_change_intervals":
        names = ("ts_code", "start_date", "name")
    else:
        names = ("index_code", "con_code", "in_date")
    if any(_is_missing(record.get(name)) for name in names):
        return None
    return {name: record[name] for name in names}


def _stable_key(key: Mapping[str, Any]) -> str:
    return repr(tuple((name, key[name]) for name in sorted(key)))


def _status_for(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    value = metadata.get("effective_status", metadata.get("status"))
    if isinstance(value, RevisionStatus):
        return value.value
    if isinstance(value, str):
        return value.strip().lower()
    if bool(metadata.get("provisional", False)):
        return RevisionStatus.PROVISIONAL.value
    return RevisionStatus.OBSERVED.value


def _decision_date(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> str | None:
    for container in (record, metadata):
        for field in ("decision_date", "trade_date", "cal_date", "start_date"):
            normalized = _normal_date(container.get(field))
            if normalized is not None:
                return normalized
    return None


def _labels(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    for container in (record, metadata):
        value = container.get("labels")
        if isinstance(value, Mapping):
            return dict(value)
        direct = {name: container.get(name) for name in LABEL_FIELDS if name in container}
        if direct:
            return direct
    return {}


def _required_fields(
    dataset: str,
    override: Iterable[str] | None,
) -> tuple[str, ...]:
    if override is None:
        selected: Iterable[str] = REQUIRED_COLUMNS[dataset]
    elif isinstance(override, str):
        selected = (override,)
    else:
        selected = override
    result = tuple(sorted(str(field) for field in selected))
    if not result:
        raise DataQualityError("required_fields cannot be empty")
    return result


def _field_names(dataset: str, records: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    names = {field.name for field in dataclass_fields(RECORD_MODELS[dataset])}
    for record in records:
        names.update(record)
    return tuple(sorted(names))


def _coverage(records: Sequence[Mapping[str, Any]], field_names: Iterable[str]) -> dict[str, Any]:
    total = len(records)
    result: dict[str, Any] = {}
    for field in field_names:
        present = sum(not _is_missing(record.get(field)) for record in records)
        ratio = present / total if total else 0.0
        result[field] = {
            "present": present,
            "total": total,
            "missing": total - present,
            "ratio": round(ratio, 6),
            "percent": round(ratio * 100.0, 4),
        }
    return result


def _missing_values(
    records: Sequence[Mapping[str, Any]],
    field_names: Iterable[str],
) -> dict[str, int]:
    return {
        field: sum(_is_missing(record.get(field)) for record in records)
        for field in field_names
    }


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    count: int = 1,
    details: Any = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "count": count,
    }
    if details is not None:
        result["details"] = details
    return result


def _duplicate_keys(
    dataset: str,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_key_rows: list[int] = []
    for index, record in enumerate(records):
        key = _key_for(dataset, record)
        if key is None:
            missing_key_rows.append(index)
            continue
        groups[_stable_key(key)].append(key)
    duplicates = [
        {"key": values[0], "count": len(values)}
        for values in groups.values()
        if len(values) > 1
    ]
    duplicate_count = sum(item["count"] - 1 for item in duplicates)
    return {
        "valid": not duplicates and not missing_key_rows,
        "duplicate_count": duplicate_count,
        "duplicate_key_count": len(duplicates),
        "unique_key_count": len(groups),
        "duplicates": duplicates[:100],
        "missing_key_rows": missing_key_rows[:100],
    }


_DATE_FIELDS_BY_DATASET = {
    "trade_calendar": ("cal_date", "pretrade_date"),
    "stock_metadata": ("list_date", "delist_date"),
    "daily_bars": ("trade_date",),
    "daily_basic_metrics": ("trade_date",),
    "adjustment_factors": ("trade_date",),
    "name_change_intervals": ("start_date", "end_date", "ann_date"),
    "sector_membership": ("in_date", "out_date"),
}


def _date_ordering(
    dataset: str,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    invalid_dates: list[dict[str, Any]] = []
    date_fields = _DATE_FIELDS_BY_DATASET[dataset]
    for index, record in enumerate(records):
        for field in date_fields:
            value = record.get(field)
            if not _is_missing(value) and _normal_date(value) is None:
                invalid_dates.append({"row": index, "field": field, "value": value})

    relation: tuple[str, str] | None = None
    if dataset == "trade_calendar":
        relation = ("pretrade_date", "cal_date")
    elif dataset == "stock_metadata":
        relation = ("list_date", "delist_date")
    elif dataset == "name_change_intervals":
        relation = ("start_date", "end_date")
    elif dataset == "sector_membership":
        relation = ("in_date", "out_date")

    if relation is not None:
        start_field, end_field = relation
        for index, record in enumerate(records):
            start = _normal_date(record.get(start_field))
            end = _normal_date(record.get(end_field))
            if start is not None and end is not None and end < start:
                violations.append(
                    {
                        "row": index,
                        "code": "end_before_start",
                        "start_field": start_field,
                        "start_date": start,
                        "end_field": end_field,
                        "end_date": end,
                    }
                )

    if dataset == "name_change_intervals":
        for index, record in enumerate(records):
            start = _normal_date(record.get("start_date"))
            announced = _normal_date(record.get("ann_date"))
            if start is not None and announced is not None and announced > start:
                violations.append(
                    {
                        "row": index,
                        "code": "announcement_after_start",
                        "ann_date": announced,
                        "start_date": start,
                    }
                )

        intervals: dict[str, list[tuple[str, str | None, int]]] = defaultdict(list)
        for index, record in enumerate(records):
            code = record.get("ts_code")
            start = _normal_date(record.get("start_date"))
            end = _normal_date(record.get("end_date"))
            if not _is_missing(code) and start is not None:
                intervals[str(code)].append((start, end, index))
        for code, values in intervals.items():
            values.sort(key=lambda item: (item[0], item[2]))
            for previous, current in zip(values, values[1:]):
                previous_end = previous[1]
                if previous_end is None or current[0] <= previous_end:
                    violations.append(
                        {
                            "row": current[2],
                            "code": "overlapping_intervals",
                            "ts_code": code,
                            "previous_start_date": previous[0],
                            "previous_end_date": previous_end,
                            "start_date": current[0],
                        }
                    )

    return {
        "valid": not violations and not invalid_dates,
        "violation_count": len(violations),
        "invalid_date_count": len(invalid_dates),
        "violations": violations[:100],
        "invalid_dates": invalid_dates[:100],
    }


def _ohlc_quality(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        prices = {
            field: _normal_number(record.get(field))
            for field in ("open", "high", "low", "close")
        }
        missing_or_invalid = [field for field, value in prices.items() if value is None]
        if missing_or_invalid:
            violations.append(
                {
                    "row": index,
                    "code": "missing_or_non_numeric_ohlc",
                    "fields": missing_or_invalid,
                }
            )
            continue
        open_price = prices["open"]
        high = prices["high"]
        low = prices["low"]
        close = prices["close"]
        assert open_price is not None
        assert high is not None
        assert low is not None
        assert close is not None
        if min(open_price, high, low, close) <= 0:
            violations.append({"row": index, "code": "non_positive_ohlc"})
        if high < max(open_price, low, close):
            violations.append(
                {
                    "row": index,
                    "code": "high_below_ohlc",
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                }
            )
        if low > min(open_price, high, close):
            violations.append(
                {
                    "row": index,
                    "code": "low_above_ohlc",
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                }
            )
        for field in ("pre_close", "vol", "amount"):
            if _is_missing(record.get(field)):
                continue
            number = _normal_number(record.get(field))
            if number is None or number < 0:
                violations.append(
                    {
                        "row": index,
                        "code": "invalid_nonnegative_field",
                        "field": field,
                        "value": record.get(field),
                    }
                )
    return {
        "valid": not violations,
        "invalid_count": len(violations),
        "violations": violations[:100],
    }


def _state_inputs(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(records)
    coverage: dict[str, Any] = {}
    invalid: list[dict[str, Any]] = []
    inferred_suspended = 0
    for canonical in STATE_FIELDS:
        present = 0
        for index, record in enumerate(records):
            tradability = record.get("tradability")
            tradability = tradability if isinstance(tradability, Mapping) else {}
            raw = None
            for alias in STATE_ALIASES[canonical]:
                if alias in tradability:
                    raw = tradability[alias]
                    break
                if alias in record:
                    raw = record[alias]
                    break
            if raw is None:
                continue
            value = _normal_bool(raw)
            if value is None:
                invalid.append(
                    {
                        "row": index,
                        "field": canonical,
                        "value": raw,
                        "code": "invalid_state_value",
                    }
                )
            else:
                present += 1
        coverage[canonical] = {
            "present": present,
            "total": total,
            "missing": total - present,
            "ratio": round(present / total, 6) if total else 0.0,
        }

    for index, record in enumerate(records):
        explicit = any(
            not _is_missing(record.get(alias))
            or (
                isinstance(record.get("tradability"), Mapping)
                and not _is_missing(record["tradability"].get(alias))
            )
            for aliases in STATE_ALIASES.values()
            for alias in aliases
        )
        if explicit:
            continue
        volume = _normal_number(record.get("vol", record.get("volume")))
        amount = _normal_number(record.get("amount"))
        if volume == 0 or amount == 0:
            inferred_suspended += 1

    missing_count = sum(item["missing"] for item in coverage.values())
    return {
        "valid": not invalid and missing_count == 0,
        "state_inputs_ready": not invalid and missing_count == 0,
        "provided_fields": [
            field for field, item in coverage.items() if item["present"] > 0
        ],
        "coverage": coverage,
        "missing_count": missing_count,
        "invalid_count": len(invalid),
        "invalid_values": invalid[:100],
        "inferred_suspended_count": inferred_suspended,
    }


def _normal_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _point_in_time_quality(
    rows: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    *,
    as_of_utc: str | None,
    required_fields: Sequence[str],
    require_point_in_time: bool,
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    missing: list[dict[str, Any]] = []
    future_information: list[dict[str, Any]] = []
    eligible_count = 0
    point_in_time_eligible_count = 0
    as_of = _normal_timestamp(as_of_utc)

    for index, (record, metadata) in enumerate(rows):
        status = _status_for(record, metadata)
        status_counts[status] += 1
        combined: dict[str, Any] = dict(record)
        pit = record.get("point_in_time", metadata.get("point_in_time"))
        if isinstance(pit, Mapping):
            combined.update(pit)
        for field in PIT_FIELDS:
            if field not in combined and field in metadata:
                combined[field] = metadata[field]
        missing_fields = [field for field in required_fields if _is_missing(combined.get(field))]
        if missing_fields:
            missing.append({"row": index, "fields": missing_fields})

        status_eligible = status in REAL_STATUSES
        future = False
        for field in ("observed_at_utc", "recorded_at_utc"):
            timestamp = _normal_timestamp(combined.get(field))
            if as_of is not None and timestamp is not None:
                observed = timestamp
                cutoff = as_of
                if observed.tzinfo is not None and cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=observed.tzinfo)
                elif observed.tzinfo is None and cutoff.tzinfo is not None:
                    observed = observed.replace(tzinfo=cutoff.tzinfo)
                if observed > cutoff:
                    future = True
                    future_information.append(
                        {
                            "row": index,
                            "field": field,
                            "observed_at": timestamp.isoformat(),
                            "as_of_utc": as_of_utc,
                        }
                    )
        if status_eligible and not future:
            point_in_time_eligible_count += 1
        if status_eligible and not future and (not require_point_in_time or not missing_fields):
            eligible_count += 1

    provisional_count = status_counts[RevisionStatus.PROVISIONAL.value]
    noneligible_count = len(rows) - eligible_count
    eligible = (
        len(rows) > 0
        and eligible_count == len(rows)
        and not future_information
        and (not require_point_in_time or not missing)
    )
    return {
        "eligible": eligible,
        "point_in_time_eligible": eligible,
        "eligible_count": eligible_count,
        "point_in_time_eligible_count": point_in_time_eligible_count,
        "noneligible_count": noneligible_count,
        "status_counts": dict(sorted(status_counts.items())),
        "provisional_count": provisional_count,
        "real_count": sum(status_counts[status] for status in REAL_STATUSES),
        "missing_count": len(missing),
        "missing_fields": missing[:100],
        "future_information_count": len(future_information),
        "future_information": future_information[:100],
        "required_fields": list(required_fields),
    }


def _label_readiness(
    rows: Sequence[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    total = len(rows)
    available_counts: Counter[str] = Counter()
    labeled_rows = 0
    missing: list[int] = []
    for index, (record, metadata) in enumerate(rows):
        labels = _labels(record, metadata)
        present = [
            name for name, value in labels.items() if not _is_missing(value)
        ]
        for name in present:
            available_counts[name] += 1
        if present:
            labeled_rows += 1
        else:
            missing.append(index)
    coverage = {
        name: {
            "present": count,
            "total": total,
            "missing": total - count,
            "ratio": round(count / total, 6) if total else 0.0,
        }
        for name, count in sorted(available_counts.items())
    }
    return {
        "ready": labeled_rows > 0,
        "fully_ready": total > 0 and labeled_rows == total,
        "ready_count": labeled_rows,
        "total": total,
        "missing_count": len(missing),
        "missing_rows": missing[:100],
        "label_fields": list(LABEL_FIELDS),
        "coverage": coverage,
    }


def _window_rows(
    rows: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    *,
    start_date: str | None,
    end_date: str | None,
    max_decision_dates: int | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if start_date is None and end_date is None and max_decision_dates is None:
        return list(rows)
    start = _normal_date(start_date) if start_date is not None else None
    end = _normal_date(end_date) if end_date is not None else None
    if start_date is not None and start is None:
        raise DataQualityError("start_date must be a valid date")
    if end_date is not None and end is None:
        raise DataQualityError("end_date must be a valid date")
    if start is not None and end is not None and end < start:
        raise DataQualityError("end_date cannot precede start_date")
    if max_decision_dates is not None and (
        isinstance(max_decision_dates, bool)
        or not isinstance(max_decision_dates, int)
        or max_decision_dates <= 0
    ):
        raise DataQualityError("max_decision_dates must be a positive integer")

    selected = []
    for record, metadata in rows:
        decision_date = _decision_date(record, metadata)
        if decision_date is None:
            continue
        if start is not None and decision_date < start:
            continue
        if end is not None and decision_date > end:
            continue
        selected.append((record, metadata))
    if max_decision_dates is not None:
        dates = sorted(
            {
                decision_date
                for record, metadata in selected
                if (decision_date := _decision_date(record, metadata)) is not None
            }
        )
        allowed = set(dates[-max_decision_dates:])
        selected = [
            (record, metadata)
            for record, metadata in selected
            if _decision_date(record, metadata) in allowed
        ]
    return selected


def select_decision_window(
    records: Sequence[Any] | Iterable[Any],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    max_decision_dates: int | None = None,
) -> list[Any]:
    """Return the shortened decision window used by development audits."""
    supplied = list(records)
    annotated = []
    for item in supplied:
        record, metadata = _unwrap(item)
        annotated.append((item, record, metadata))
    selected = _window_rows(
        [(record, metadata) for _, record, metadata in annotated],
        start_date=start_date,
        end_date=end_date,
        max_decision_dates=max_decision_dates,
    )
    selected_dates = Counter(
        _decision_date(record, metadata) for record, metadata in selected
    )
    result = []
    for item, record, metadata in annotated:
        decision_date = _decision_date(record, metadata)
        if decision_date is not None and selected_dates[decision_date] > 0:
            result.append(item)
            selected_dates[decision_date] -= 1
    return result


def audit_records(
    dataset: str,
    records: Sequence[Any] | Iterable[Any],
    *,
    status: RevisionStatus | str | None = None,
    as_of_utc: str | None = None,
    required_fields: Iterable[str] | None = None,
    point_in_time_fields: Sequence[str] = ("decision_date", "observed_at_utc"),
    require_state_inputs: bool = False,
    require_point_in_time: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    max_decision_dates: int | None = None,
) -> dict[str, Any]:
    """Return a deterministic quality artifact for a sequence of records."""
    try:
        resolved_dataset = normalize_dataset(dataset)
    except Exception as exc:
        raise DataQualityError(str(exc)) from exc
    if isinstance(records, (BaseRecord, Mapping)):
        supplied_records = [records]
    elif isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        raise DataQualityError("records must be a record, mapping, or iterable")
    else:
        supplied_records = list(records)

    rows = []
    for item in supplied_records:
        record, metadata = _unwrap(item)
        if status is not None:
            metadata = dict(metadata)
            metadata["status"] = (
                status.value if isinstance(status, RevisionStatus) else str(status)
            )
        rows.append((record, metadata))
    rows = _window_rows(
        rows,
        start_date=start_date,
        end_date=end_date,
        max_decision_dates=max_decision_dates,
    )
    record_maps = [{**metadata, **record} for record, metadata in rows]
    required = _required_fields(resolved_dataset, required_fields)
    all_fields = _field_names(resolved_dataset, record_maps)
    required_coverage = _coverage(record_maps, required)
    all_coverage = _coverage(record_maps, all_fields)
    missing_values = _missing_values(record_maps, all_fields)
    missing_required = [
        {
            "row": index,
            "fields": [field for field in required if _is_missing(record.get(field))],
        }
        for index, record in enumerate(record_maps)
        if any(_is_missing(record.get(field)) for field in required)
    ]
    duplicates = _duplicate_keys(resolved_dataset, record_maps)
    dates = _date_ordering(resolved_dataset, record_maps)
    ohlc = (
        _ohlc_quality(record_maps)
        if resolved_dataset == "daily_bars"
        else {
            "valid": True,
            "invalid_count": 0,
            "violations": [],
            "applicable": False,
        }
    )
    states = (
        _state_inputs(record_maps)
        if resolved_dataset == "daily_bars"
        else {
            "valid": True,
            "state_inputs_ready": True,
            "provided_fields": [],
            "coverage": {},
            "missing_count": 0,
            "invalid_count": 0,
            "invalid_values": [],
            "inferred_suspended_count": 0,
            "applicable": False,
        }
    )
    labels = _label_readiness(rows)
    pit = _point_in_time_quality(
        rows,
        as_of_utc=as_of_utc,
        required_fields=tuple(point_in_time_fields),
        require_point_in_time=require_point_in_time,
    )
    decision_dates = sorted(
        {
            decision_date
            for record, metadata in rows
            if (decision_date := _decision_date(record, metadata)) is not None
        }
    )

    issues: list[dict[str, Any]] = []
    if missing_required:
        issues.append(
            _issue(
                "missing_required_fields",
                "One or more records omit required fields.",
                count=len(missing_required),
                details=missing_required[:100],
            )
        )
    if not duplicates["valid"]:
        issues.append(
            _issue(
                "duplicate_keys",
                "Natural keys must be unique within a quality snapshot.",
                count=duplicates["duplicate_count"],
                details=duplicates["duplicates"],
            )
        )
    if not dates["valid"]:
        issues.append(
            _issue(
                "date_ordering",
                "One or more date relationships are invalid.",
                count=dates["violation_count"] + dates["invalid_date_count"],
                details={
                    "violations": dates["violations"],
                    "invalid_dates": dates["invalid_dates"],
                },
            )
        )
    if not ohlc["valid"]:
        issues.append(
            _issue(
                "invalid_ohlc_relationships",
                "One or more daily bars have invalid OHLC values.",
                count=ohlc["invalid_count"],
                details=ohlc["violations"],
            )
        )
    if states["invalid_count"]:
        issues.append(
            _issue(
                "invalid_suspended_limit_state_inputs",
                "One or more tradability state inputs are invalid.",
                count=states["invalid_count"],
                details=states["invalid_values"],
            )
        )
    if require_state_inputs and not states["state_inputs_ready"]:
        issues.append(
            _issue(
                "missing_suspended_limit_state_inputs",
                "Suspended and limit-state inputs are required for this audit.",
                count=states["missing_count"],
            )
        )
    if require_point_in_time and not pit["eligible"]:
        issues.append(
            _issue(
                "point_in_time_ineligible",
                "Records are not fully point-in-time eligible.",
                count=pit["noneligible_count"],
                details={
                    "missing": pit["missing_fields"],
                    "future_information": pit["future_information"],
                },
            )
        )
    if not labels["ready"]:
        issues.append(
            _issue(
                "labels_not_ready",
                "No labels are available in the audited records.",
                count=labels["missing_count"],
                details=labels["missing_rows"],
            )
        )
    if pit["provisional_count"]:
        issues.append(
            _issue(
                "provisional_records_present",
                "Provisional records are visible but are not release eligible.",
                severity="warning",
                count=pit["provisional_count"],
            )
        )

    critical_codes = {
        "missing_required_fields",
        "duplicate_keys",
        "date_ordering",
        "invalid_ohlc_relationships",
        "invalid_suspended_limit_state_inputs",
    }
    passed = not any(
        issue["code"] in critical_codes and issue["severity"] == "error"
        for issue in issues
    )
    if require_state_inputs and not states["state_inputs_ready"]:
        passed = False
    if require_point_in_time and not pit["eligible"]:
        passed = False

    result = {
        "artifact_type": "kinace_v3_data_quality",
        "schema_version": QUALITY_SCHEMA_VERSION,
        "dataset": resolved_dataset,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "valid": passed,
        "record_count": len(record_maps),
        "unique_key_count": duplicates["unique_key_count"],
        "decision_date_count": len(decision_dates),
        "decision_dates": decision_dates,
        "required_fields": list(required),
        "required_field_coverage": required_coverage,
        "required_field_coverage_ratio": (
            min(float(item["ratio"]) for item in required_coverage.values())
            if required_coverage
            else 0.0
        ),
        "field_coverage": all_coverage,
        "missing_values": missing_values,
        "missing_value_count": sum(missing_values.values()),
        "missing_required": missing_required[:100],
        "date_ordering": dates,
        "date_order_valid": dates["valid"],
        "invalid_ohlc_relationships": ohlc,
        "invalid_ohlc_count": ohlc["invalid_count"],
        "duplicate_keys": duplicates,
        "duplicate_key_count": duplicates["duplicate_count"],
        "suspended_limit_state_inputs": states,
        "state_inputs_ready": states["state_inputs_ready"],
        "label_readiness": labels,
        "labels_ready": labels["ready"],
        "point_in_time_eligibility": pit,
        "point_in_time_eligible": pit["eligible"],
        "as_of_utc": as_of_utc,
        "issues": issues,
        "failures": issues,
    }
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


quality_audit = audit_records
compute_quality_report = audit_records
quality_report = audit_records
quality_check = audit_records


def audit_store(
    store_or_database: RevisionStore | str | Path | None,
    dataset: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Audit current revisions from the append-only store."""
    store = (
        store_or_database
        if isinstance(store_or_database, RevisionStore)
        else RevisionStore(store_or_database)
    )
    if dataset is None:
        datasets = [
            audit_store(store, name, **kwargs)
            for name in sorted(RECORD_MODELS)
        ]
        return {
            "artifact_type": "kinace_v3_data_quality_collection",
            "schema_version": QUALITY_SCHEMA_VERSION,
            "datasets": {item["dataset"]: item for item in datasets},
            "record_count": sum(item["record_count"] for item in datasets),
            "passed": all(item["passed"] for item in datasets),
        }
    try:
        resolved_dataset = normalize_dataset(dataset)
    except Exception as exc:
        raise DataQualityError(str(exc)) from exc
    records = store.current_records(resolved_dataset)
    return audit_records(resolved_dataset, records, **kwargs)


def quality_report(
    subject: RevisionStore | str | Path | Mapping[str, Any] | Sequence[Any],
    dataset: str | Sequence[Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Audit a store or records using either supported argument order."""
    is_store = isinstance(subject, (RevisionStore, Path)) or (
        isinstance(subject, str) and subject.lower().endswith((".sqlite3", ".db"))
    )
    if is_store:
        if dataset is not None and not isinstance(dataset, str):
            raise DataQualityError("dataset must be a string when auditing a store")
        return audit_store(subject, dataset, **kwargs)
    if not isinstance(dataset, str):
        raise DataQualityError("dataset is required when auditing records")
    return audit_records(dataset, subject, **kwargs)


quality_audit = quality_report
quality_check = quality_report
compute_quality_report = quality_report


def evaluate_quality_eligibility(
    report: Mapping[str, Any],
    *,
    mode: str = "release",
    min_decision_dates: int | None = None,
    max_decision_dates: int | None = None,
    decision_window: int | Mapping[str, Any] | Sequence[str] | None = None,
    allow_provisional: bool | None = None,
    require_labels: bool | None = None,
    require_state_inputs: bool | None = None,
    require_point_in_time: bool | None = None,
    release_min_decision_dates: int = RELEASE_MIN_DECISION_DATES,
    development_min_decision_dates: int = DEVELOPMENT_MIN_DECISION_DATES,
) -> dict[str, Any]:
    """Evaluate a quality report under development or release policy."""
    if not isinstance(report, Mapping):
        raise DataQualityError("quality report must be an object")
    if decision_window is not None:
        if isinstance(decision_window, bool):
            raise DataQualityError("decision_window must be an integer or date sequence")
        if isinstance(decision_window, int):
            max_decision_dates = decision_window
        elif isinstance(decision_window, Mapping):
            max_decision_dates = decision_window.get(
                "max_decision_dates", max_decision_dates
            )
        elif isinstance(decision_window, Sequence) and not isinstance(
            decision_window, (str, bytes)
        ):
            max_decision_dates = len(decision_window)
        else:
            raise DataQualityError("decision_window must be an integer or date sequence")
    resolved_mode = str(mode).strip().lower()
    resolved_mode = {"dev": "development", "prod": "release"}.get(
        resolved_mode, resolved_mode
    )
    if resolved_mode not in {"development", "release"}:
        raise DataQualityError("mode must be development or release")
    development = resolved_mode == "development"
    allow_provisional = development if allow_provisional is None else allow_provisional
    require_labels = (not development) if require_labels is None else require_labels
    require_state_inputs = (
        (not development) if require_state_inputs is None else require_state_inputs
    )
    require_point_in_time = (
        (not development) if require_point_in_time is None else require_point_in_time
    )

    decision_date_count = int(report.get("decision_date_count") or 0)
    if min_decision_dates is None:
        if max_decision_dates is not None:
            if (
                isinstance(max_decision_dates, bool)
                or not isinstance(max_decision_dates, int)
                or max_decision_dates <= 0
            ):
                raise DataQualityError("max_decision_dates must be a positive integer")
            minimum_dates = development_min_decision_dates
        else:
            minimum_dates = (
                development_min_decision_dates
                if development
                else release_min_decision_dates
            )
    else:
        if isinstance(min_decision_dates, bool) or min_decision_dates <= 0:
            raise DataQualityError("min_decision_dates must be a positive integer")
        minimum_dates = int(min_decision_dates)

    required_coverage = report.get("required_field_coverage")
    coverage_failures = []
    if isinstance(required_coverage, Mapping):
        coverage_failures = sorted(
            field
            for field, item in required_coverage.items()
            if not isinstance(item, Mapping) or float(item.get("ratio", 1.0)) < 1.0
        )
    duplicate_count = int(
        (report.get("duplicate_keys") or {}).get("duplicate_count", 0)
    )
    date_valid = bool((report.get("date_ordering") or {}).get("valid", True))
    ohlc = report.get("invalid_ohlc_relationships") or {}
    ohlc_valid = bool(ohlc.get("valid", True))
    states = report.get("suspended_limit_state_inputs") or {}
    state_ready = bool(states.get("state_inputs_ready", not require_state_inputs))
    labels = report.get("label_readiness") or {}
    labels_ready = bool(labels.get("ready", not require_labels))
    pit = report.get("point_in_time_eligibility") or {}
    pit_eligible = bool(pit.get("eligible", False))
    provisional_count = int(pit.get("provisional_count", 0))
    record_count = int(report.get("record_count") or 0)

    blockers: list[str] = []
    if record_count <= 0:
        blockers.append("no_records")
    if coverage_failures:
        blockers.append("required_field_coverage")
    if duplicate_count:
        blockers.append("duplicate_keys")
    if not date_valid:
        blockers.append("date_ordering")
    if not ohlc_valid:
        blockers.append("invalid_ohlc_relationships")
    if require_state_inputs and not state_ready:
        blockers.append("suspended_limit_state_inputs")
    if require_labels and not labels_ready:
        blockers.append("label_readiness")
    if require_point_in_time and not pit_eligible:
        blockers.append("point_in_time_eligibility")
    if not allow_provisional and provisional_count:
        blockers.append("provisional_records")
    if not report.get("passed", True):
        blockers.append("quality_failed")

    development_blockers = []
    if record_count <= 0:
        development_blockers.append("no_records")
    if coverage_failures:
        development_blockers.append("required_field_coverage")
    if duplicate_count:
        development_blockers.append("duplicate_keys")
    if not date_valid:
        development_blockers.append("date_ordering")
    if not ohlc_valid:
        development_blockers.append("invalid_ohlc_relationships")
    if decision_date_count < minimum_dates:
        development_blockers.append("insufficient_decision_dates")

    release_blockers = list(blockers)
    if decision_date_count < minimum_dates:
        release_blockers.append("insufficient_decision_dates")
    release_eligible = development is False and not release_blockers
    development_eligible = not development_blockers
    return {
        "mode": resolved_mode,
        "development_only": development,
        "development_eligible": development_eligible,
        "release_eligible": release_eligible,
        "production_eligible": release_eligible,
        "production_ready": release_eligible,
        "decision_date_count": decision_date_count,
        "minimum_decision_dates": minimum_dates,
        "max_decision_dates": max_decision_dates,
        "shortened_decision_window": max_decision_dates is not None,
        "allow_provisional": allow_provisional,
        "require_labels": require_labels,
        "require_state_inputs": require_state_inputs,
        "require_point_in_time": require_point_in_time,
        "provisional_count": provisional_count,
        "coverage_failures": coverage_failures,
        "blockers": sorted(set(release_blockers if not development else development_blockers)),
        "release_blockers": sorted(set(release_blockers)),
        "development_blockers": sorted(set(development_blockers)),
    }


def is_release_eligible(
    report: Mapping[str, Any],
    **kwargs: Any,
) -> bool:
    return bool(
        evaluate_quality_eligibility(report, mode="release", **kwargs)[
            "release_eligible"
        ]
    )


def is_development_eligible(
    report: Mapping[str, Any],
    **kwargs: Any,
) -> bool:
    return bool(
        evaluate_quality_eligibility(report, mode="development", **kwargs)[
            "development_eligible"
        ]
    )


quality_eligibility = evaluate_quality_eligibility
evaluate_eligibility = evaluate_quality_eligibility


def evaluate_release_eligibility(
    report: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    return evaluate_quality_eligibility(report, mode="release", **kwargs)


def evaluate_development_eligibility(
    report: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    return evaluate_quality_eligibility(report, mode="development", **kwargs)


run_quality_checks = audit_store
run_quality_check = audit_store
assess_quality = audit_records
compute_quality_metrics = audit_records


__all__ = [
    "DEVELOPMENT_MIN_DECISION_DATES",
    "DataQualityError",
    "LABEL_FIELDS",
    "PIT_FIELDS",
    "QUALITY_SCHEMA_VERSION",
    "REAL_STATUSES",
    "RELEASE_MIN_DECISION_DATES",
    "STATE_FIELDS",
    "audit_records",
    "audit_store",
    "assess_quality",
    "compute_quality_metrics",
    "compute_quality_report",
    "evaluate_development_eligibility",
    "evaluate_eligibility",
    "evaluate_quality_eligibility",
    "evaluate_release_eligibility",
    "is_development_eligible",
    "is_release_eligible",
    "quality_audit",
    "quality_check",
    "quality_eligibility",
    "quality_report",
    "run_quality_check",
    "run_quality_checks",
    "select_decision_window",
]
