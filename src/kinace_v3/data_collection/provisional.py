"""Provisional baseline fills that never count as release-eligible data."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ingest import IngestionResult, ingest_records
from .models import (
    BaseRecord,
    DailyBarRecord,
    RevisionStatus,
    normalize_dataset,
)
from .store import RevisionStore


PROVISIONAL_SOURCE = "provisional-baseline"


class ProvisionalError(ValueError):
    """Raised when a provisional baseline is malformed or misused."""


@dataclass(frozen=True)
class ProvisionalFillResult(IngestionResult):
    """Ingestion summary with explicit non-eligibility markers."""

    @property
    def gate_eligible(self) -> bool:
        return False

    @property
    def training_eligible(self) -> bool:
        return False

    @property
    def release_eligible(self) -> bool:
        return False

    @property
    def provisional(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update(
            {
                "provisional": True,
                "gate_eligible": False,
                "training_eligible": False,
                "release_eligible": False,
            }
        )
        return result


ProvisionalResult = ProvisionalFillResult


def _finite_price(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProvisionalError("baseline price must be a finite number")
    number = float(value)
    if number <= 0:
        raise ProvisionalError("baseline price must be positive")
    return number


def _baseline_values(
    natural_key: Mapping[str, Any],
    baseline: Any,
    values: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(natural_key, Mapping):
        raise ProvisionalError("provisional natural key must be an object")
    result = dict(natural_key)
    if isinstance(baseline, Mapping):
        result.update(baseline)
    elif baseline is not None:
        price = _finite_price(baseline)
        result.update(
            {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "pre_close": price,
                "change": 0.0,
                "pct_chg": 0.0,
                "vol": 0.0,
                "amount": 0.0,
            }
        )
    result.update(dict(values or {}))
    return result


def build_provisional_record(
    dataset: str,
    natural_key: Mapping[str, Any],
    *,
    baseline: Any = None,
    values: Mapping[str, Any] | None = None,
) -> BaseRecord | dict[str, Any]:
    """Build one explicit provisional record from a key and baseline values."""
    resolved = normalize_dataset(dataset)
    candidate = _baseline_values(natural_key, baseline, values)
    if resolved == "daily_bars":
        required = ("ts_code", "trade_date", "open", "high", "low", "close")
        missing = [field for field in required if field not in candidate]
        if missing:
            raise ProvisionalError(
                "provisional daily bar is missing fields: " + ", ".join(missing)
            )
        return DailyBarRecord.from_dict(candidate)
    return candidate


def fill_provisional(
    store: RevisionStore | str | Path | None,
    dataset: str,
    records_or_keys: BaseRecord
    | Mapping[str, Any]
    | Iterable[BaseRecord | Mapping[str, Any]]
    | None = None,
    *,
    records: BaseRecord
    | Mapping[str, Any]
    | Iterable[BaseRecord | Mapping[str, Any]]
    | None = None,
    keys: Iterable[Mapping[str, Any]] | None = None,
    baseline: Any = None,
    numeric_baseline: Any = None,
    values: Mapping[str, Any] | None = None,
    source: str = PROVISIONAL_SOURCE,
    run_metadata: Mapping[str, Any] | None = None,
) -> ProvisionalFillResult:
    """Fill missing natural keys with provisional records.

    Existing real revisions are never replaced by this function. Each generated
    revision is status ``provisional`` and therefore has both eligibility flags
    set to false in the current views.
    """
    if baseline is not None and numeric_baseline is not None:
        raise ProvisionalError("provide only one of baseline or numeric_baseline")
    if numeric_baseline is not None:
        baseline = numeric_baseline
    if keys is not None and (records is not None or records_or_keys is not None):
        raise ProvisionalError("provide records or keys, not both")
    selected = keys if keys is not None else records if records is not None else records_or_keys
    if selected is None:
        raise ProvisionalError("records or natural keys are required")
    if isinstance(selected, (BaseRecord, Mapping)):
        supplied = [selected]
    else:
        supplied = list(selected)

    if baseline is not None:
        generated = []
        for item in supplied:
            if isinstance(item, BaseRecord):
                key = item.natural_key()
            elif isinstance(item, Mapping):
                key = dict(item)
            else:
                raise ProvisionalError("provisional keys must be objects")
            generated.append(
                build_provisional_record(
                    dataset,
                    key,
                    baseline=baseline,
                    values=values,
                )
            )
        supplied = generated
    elif values is not None:
        raise ProvisionalError("values require baseline")

    result = ingest_records(
        store,
        dataset,
        supplied,
        status=RevisionStatus.PROVISIONAL,
        source=source,
        run_metadata={"operation": "provisional_fill", **dict(run_metadata or {})},
    )
    return ProvisionalFillResult(
        dataset=result.dataset,
        source=result.source,
        run_id=result.run_id,
        status=result.status,
        records_seen=result.records_seen,
        inserted_count=result.inserted_count,
        skipped_count=result.skipped_count,
        superseded_count=result.superseded_count,
        revision_ids=result.revision_ids,
    )


def create_provisional_baseline(
    store: RevisionStore | str | Path | None,
    dataset: str,
    records_or_keys: Any = None,
    **kwargs: Any,
) -> ProvisionalFillResult:
    return fill_provisional(store, dataset, records_or_keys, **kwargs)


def provisional_eligibility(status: RevisionStatus | str) -> dict[str, bool]:
    try:
        resolved = status if isinstance(status, RevisionStatus) else RevisionStatus(status)
    except ValueError:
        resolved = RevisionStatus.INVALID
    eligible = resolved in {RevisionStatus.OBSERVED, RevisionStatus.BACKFILLED_RELIABLE}
    return {
        "provisional": resolved == RevisionStatus.PROVISIONAL,
        "training_eligible": eligible,
        "gate_eligible": eligible,
        "release_eligible": eligible,
    }


provisional_fill = fill_provisional
fill_baseline = fill_provisional
create_baseline = fill_provisional
apply_provisional_baseline = fill_provisional


__all__ = [
    "PROVISIONAL_SOURCE",
    "ProvisionalError",
    "ProvisionalFillResult",
    "ProvisionalResult",
    "build_provisional_record",
    "apply_provisional_baseline",
    "create_baseline",
    "create_provisional_baseline",
    "fill_baseline",
    "fill_provisional",
    "provisional_eligibility",
    "provisional_fill",
]
