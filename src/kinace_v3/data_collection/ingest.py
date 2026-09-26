"""Provider-neutral ingestion orchestration for immutable data revisions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    BaseRecord,
    RevisionStatus,
    natural_key_for,
    normalize_dataset,
    normalize_record,
)
from .revisions import RevisionError, normalize_status
from .store import RevisionStore, RevisionStoreError


class IngestionError(ValueError):
    """Raised when provider records cannot be ingested safely."""


_REAL_PROVIDER_STATUSES = frozenset(
    {RevisionStatus.OBSERVED, RevisionStatus.BACKFILLED_RELIABLE}
)
_INGESTIBLE_STATUSES = frozenset(
    {
        RevisionStatus.OBSERVED,
        RevisionStatus.BACKFILLED_RELIABLE,
        RevisionStatus.PROVISIONAL,
        RevisionStatus.INVALID,
    }
)
_STATUS_RANK = {
    RevisionStatus.INVALID: 0,
    RevisionStatus.PROVISIONAL: 1,
    RevisionStatus.OBSERVED: 2,
    RevisionStatus.BACKFILLED_RELIABLE: 3,
}


@dataclass(frozen=True)
class IngestionResult(Mapping[str, Any]):
    """Summary of one idempotent ingestion operation."""

    dataset: str
    source: str
    run_id: str
    status: str
    records_seen: int
    inserted_count: int
    skipped_count: int
    superseded_count: int
    revision_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "run_id": self.run_id,
            "status": self.status,
            "records_seen": self.records_seen,
            "inserted_count": self.inserted_count,
            "inserted": self.inserted_count,
            "skipped_count": self.skipped_count,
            "skipped": self.skipped_count,
            "superseded_count": self.superseded_count,
            "superseded": self.superseded_count,
            "revision_count": len(self.revision_ids),
            "revision_ids": list(self.revision_ids),
        }

    as_dict = to_dict

    @property
    def inserted(self) -> int:
        return self.inserted_count

    @property
    def skipped(self) -> int:
        return self.skipped_count

    @property
    def superseded(self) -> int:
        return self.superseded_count

    @property
    def gate_eligible(self) -> bool:
        return self.status in {
            RevisionStatus.OBSERVED.value,
            RevisionStatus.BACKFILLED_RELIABLE.value,
        }

    @property
    def training_eligible(self) -> bool:
        return self.gate_eligible

    @property
    def release_eligible(self) -> bool:
        return self.gate_eligible

    @property
    def provisional(self) -> bool:
        return self.status == RevisionStatus.PROVISIONAL.value

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


IngestResult = IngestionResult


def _record_sequence(
    records: BaseRecord | Mapping[str, Any] | Iterable[Any],
) -> list[BaseRecord | Mapping[str, Any]]:
    if isinstance(records, (BaseRecord, Mapping)):
        return [records]
    if isinstance(records, (str, bytes)):
        raise IngestionError("records must be a record, mapping, or iterable of records")
    try:
        return list(records)
    except TypeError as exc:
        raise IngestionError(
            "records must be a record, mapping, or iterable of records"
        ) from exc


def _resolve_dataset(
    dataset: str | None,
    records: Sequence[BaseRecord | Mapping[str, Any]],
) -> str:
    if dataset is not None:
        try:
            return normalize_dataset(dataset)
        except Exception as exc:
            raise IngestionError(str(exc)) from exc
    inferred = {
        record.dataset for record in records if isinstance(record, BaseRecord)
    }
    if len(inferred) == 1 and all(isinstance(record, BaseRecord) for record in records):
        return next(iter(inferred))
    raise IngestionError("dataset is required when records are not canonical models")


def _resolve_store(
    store_source: RevisionStore | str | Path | None,
    *,
    store: RevisionStore | str | Path | None,
    database: str | Path | None,
) -> RevisionStore:
    if store is not None and store_source is not None:
        raise IngestionError("provide only one store source")
    if database is not None and (store is not None or store_source is not None):
        raise IngestionError("provide only one store source")
    source = store if store is not None else database if database is not None else store_source
    if isinstance(source, RevisionStore):
        return source
    return RevisionStore(source)


def _normalize_ingestion_status(status: RevisionStatus | str) -> RevisionStatus:
    try:
        resolved = normalize_status(status)
    except RevisionError as exc:
        raise IngestionError(str(exc)) from exc
    if resolved not in _INGESTIBLE_STATUSES:
        raise IngestionError(
            f"status {resolved.value!r} cannot be ingested; use observed, "
            "backfilled_reliable, provisional, or invalid"
        )
    return resolved


def _status_value(value: Any) -> RevisionStatus | None:
    try:
        return normalize_status(value)
    except RevisionError:
        return None


def _same_record(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return dict(left) == dict(right)


def _ingestion_decision(
    current: Mapping[str, Any] | None,
    *,
    incoming_record: Mapping[str, Any],
    incoming_status: RevisionStatus,
) -> tuple[bool, bool]:
    """Return ``(should_write, supersedes_current)`` for one natural key."""
    if current is None:
        return True, False

    current_record = current.get("record")
    if not isinstance(current_record, Mapping):
        raise IngestionError("current revision is missing its canonical record")
    current_status = _status_value(current.get("effective_status", current.get("status")))
    if current_status is None:
        raise IngestionError("current revision has an invalid status")

    record_changed = not _same_record(current_record, incoming_record)
    if incoming_status == RevisionStatus.PROVISIONAL and current_status != RevisionStatus.INVALID:
        # Provisional data may fill a gap, but it must never replace or downgrade real data.
        return False, False

    if not record_changed and incoming_status == current_status:
        return False, False

    if not record_changed:
        if current_status == RevisionStatus.PROVISIONAL and incoming_status in _REAL_PROVIDER_STATUSES:
            return True, True
        if (
            incoming_status in _REAL_PROVIDER_STATUSES
            and current_status in _REAL_PROVIDER_STATUSES
            and _STATUS_RANK[incoming_status] > _STATUS_RANK[current_status]
        ):
            return True, True
        return False, False

    if current_status == RevisionStatus.PROVISIONAL and incoming_status in _REAL_PROVIDER_STATUSES:
        return True, True
    return True, True


def ingest_records(
    records_or_store: RevisionStore
    | str
    | Path
    | BaseRecord
    | Mapping[str, Any]
    | Iterable[Any]
    | None,
    dataset: str | Sequence[BaseRecord | Mapping[str, Any]] | None = None,
    records: BaseRecord | Mapping[str, Any] | Iterable[Any] | None = None,
    *,
    store: RevisionStore | str | Path | None = None,
    database: str | Path | None = None,
    status: RevisionStatus | str = RevisionStatus.OBSERVED,
    source: str = "provider",
    observed_at_utc: str | None = None,
    recorded_at_utc: str | None = None,
    run_id: str | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    bulk: bool = False,
    reject_duplicates: bool = True,
) -> IngestionResult:
    """Persist canonical provider records using append-only revisions.

    Supported call forms include ``ingest_records(store, dataset, records)`` and
    ``ingest_records(records, dataset=dataset, store=store)``.
    """

    store_source: RevisionStore | str | Path | None
    if records is not None:
        if isinstance(records_or_store, (RevisionStore, Path, str)) or records_or_store is None:
            store_source = records_or_store
        else:
            store_source = store
        dataset_arg = dataset if isinstance(dataset, str) else None
        source_records = records
    elif isinstance(records_or_store, (RevisionStore, Path)) or records_or_store is None:
        store_source = records_or_store
        dataset_arg = dataset if isinstance(dataset, str) else None
        source_records = dataset if not isinstance(dataset, str) else None
    elif (
        isinstance(records_or_store, str)
        and not isinstance(dataset, str)
        and (store is not None or database is not None)
    ):
        store_source = store
        dataset_arg = records_or_store
        source_records = dataset
    else:
        store_source = store
        dataset_arg = dataset if isinstance(dataset, str) else None
        source_records = records_or_store

    if source_records is None:
        raise IngestionError("records are required")
    resolved_store = _resolve_store(store_source, store=store, database=database)
    sequence = _record_sequence(source_records)
    resolved_dataset = _resolve_dataset(dataset_arg, sequence)
    resolved_status = _normalize_ingestion_status(status)
    if not isinstance(source, str) or not source.strip():
        raise IngestionError("source must be a non-empty string")
    resolved_source = source.strip()

    try:
        normalized_records = [normalize_record(resolved_dataset, item) for item in sequence]
        natural_keys = [
            natural_key_for(resolved_dataset, item) for item in normalized_records
        ]
    except Exception as exc:
        raise IngestionError(str(exc)) from exc

    seen_keys: dict[str, dict[str, Any]] = {}
    for key, record in zip(natural_keys, normalized_records):
        key_json = repr(sorted(key.items()))
        previous = seen_keys.get(key_json)
        if previous is not None:
            if reject_duplicates:
                raise IngestionError(f"duplicate natural key in ingestion batch: {key!r}")
            if previous != record:
                raise IngestionError(
                    f"conflicting duplicate natural key in ingestion batch: {key!r}"
                )
        seen_keys[key_json] = record

    if bulk:
        resolved_run_id = resolved_store.start_ingestion_run(
            source=resolved_source,
            metadata={
                "operation": "bulk_ingest",
                "dataset": resolved_dataset,
                "requested_status": resolved_status.value,
                **dict(run_metadata or {}),
            },
            started_at_utc=observed_at_utc,
            run_id=run_id,
        )
        try:
            summary = resolved_store.append_revisions(
                normalized_records,
                dataset=resolved_dataset,
                natural_keys=natural_keys,
                status=resolved_status,
                source=resolved_source,
                observed_at_utc=observed_at_utc,
                recorded_at_utc=recorded_at_utc,
                ingestion_run_id=resolved_run_id,
                run_metadata=run_metadata,
            )
            resolved_store.complete_ingestion_run(
                resolved_run_id,
                status="completed",
                metadata={
                    "operation": "bulk_ingest",
                    "dataset": resolved_dataset,
                    "records_seen": len(normalized_records),
                    "inserted_count": summary["inserted_count"],
                    "skipped_count": summary["skipped_count"],
                    "superseded_count": 0,
                    "revision_ids": summary["revision_ids"],
                },
            )
        except Exception as exc:
            try:
                resolved_store.complete_ingestion_run(
                    resolved_run_id,
                    status="failed",
                    metadata={"operation": "bulk_ingest", "error_type": type(exc).__name__},
                )
            except Exception:
                pass
            if isinstance(exc, IngestionError):
                raise
            if isinstance(exc, RevisionStoreError):
                raise IngestionError(str(exc)) from exc
            raise
        return IngestionResult(
            dataset=resolved_dataset,
            source=resolved_source,
            run_id=resolved_run_id,
            status=resolved_status.value,
            records_seen=len(normalized_records),
            inserted_count=summary["inserted_count"],
            skipped_count=summary["skipped_count"],
            superseded_count=0,
            revision_ids=tuple(summary["revision_ids"]),
        )

    resolved_run_id = resolved_store.start_ingestion_run(
        source=resolved_source,
        metadata={
            "operation": "ingest",
            "dataset": resolved_dataset,
            "requested_status": resolved_status.value,
            **dict(run_metadata or {}),
        },
        started_at_utc=observed_at_utc,
        run_id=run_id,
    )
    inserted = 0
    skipped = 0
    superseded = 0
    revision_ids: list[str] = []
    try:
        for natural_key, canonical in zip(natural_keys, normalized_records):
            current = resolved_store.current_revision(resolved_dataset, natural_key)
            should_write, replaces_current = _ingestion_decision(
                current,
                incoming_record=canonical,
                incoming_status=resolved_status,
            )
            if not should_write:
                skipped += 1
                continue
            revision_id = resolved_store.append_revision(
                canonical,
                dataset=resolved_dataset,
                natural_key=natural_key,
                status=resolved_status,
                source=resolved_source,
                observed_at_utc=observed_at_utc,
                recorded_at_utc=recorded_at_utc,
                ingestion_run_id=resolved_run_id,
                supersedes_revision_id=(
                    str(current["revision_id"])
                    if replaces_current and current is not None
                    else None
                ),
            )
            inserted += 1
            if replaces_current and current is not None:
                superseded += 1
            revision_ids.append(revision_id)
        resolved_store.complete_ingestion_run(
            resolved_run_id,
            status="completed",
            metadata={
                "operation": "ingest",
                "dataset": resolved_dataset,
                "records_seen": len(normalized_records),
                "inserted_count": inserted,
                "skipped_count": skipped,
                "superseded_count": superseded,
                "revision_ids": revision_ids,
            },
        )
    except Exception as exc:
        try:
            resolved_store.complete_ingestion_run(
                resolved_run_id,
                status="failed",
                metadata={"operation": "ingest", "error_type": type(exc).__name__},
            )
        except Exception:
            pass
        if isinstance(exc, IngestionError):
            raise
        if isinstance(exc, RevisionStoreError):
            raise IngestionError(str(exc)) from exc
        raise

    return IngestionResult(
        dataset=resolved_dataset,
        source=resolved_source,
        run_id=resolved_run_id,
        status=resolved_status.value,
        records_seen=len(normalized_records),
        inserted_count=inserted,
        skipped_count=skipped,
        superseded_count=superseded,
        revision_ids=tuple(revision_ids),
    )


_PROVIDER_FETCH_METHODS = {
    "trade_calendar": ("fetch_trade_calendar", "trade_cal"),
    "stock_metadata": ("fetch_stock_metadata", "stock_basic"),
    "daily_bars": ("fetch_daily_bars", "daily"),
    "daily_basic_metrics": ("fetch_daily_basic_metrics", "daily_basic"),
    "adjustment_factors": ("fetch_adjustment_factors", "adj_factor"),
    "name_change_intervals": ("fetch_name_change_intervals", "namechange"),
    "sector_membership": ("fetch_sector_membership", "index_member"),
    "security_state": ("fetch_security_state", "security_states"),
    "forward_labels": ("fetch_forward_labels", "forward_label", "labels"),
}


def ingest_dataset(
    store: RevisionStore | str | Path | None,
    provider_or_dataset: Any = None,
    dataset_or_provider: Any = None,
    *,
    dataset: str | None = None,
    provider: Any = None,
    status: RevisionStatus | str = RevisionStatus.OBSERVED,
    source: str | None = None,
    fetch_kwargs: Mapping[str, Any] | None = None,
    observed_at_utc: str | None = None,
    recorded_at_utc: str | None = None,
    run_id: str | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    bulk: bool = False,
) -> IngestionResult:
    """Fetch one dataset from a provider and ingest its normalized records."""
    if provider is not None:
        if dataset is None:
            raise IngestionError("dataset is required when provider is supplied")
        resolved_provider = provider
    elif dataset is not None:
        resolved_provider = provider_or_dataset
    elif isinstance(provider_or_dataset, str):
        dataset = provider_or_dataset
        resolved_provider = dataset_or_provider
    else:
        resolved_provider = provider_or_dataset
        dataset = dataset_or_provider
    try:
        resolved_dataset = normalize_dataset(dataset)
    except Exception as exc:
        raise IngestionError(str(exc)) from exc

    fetch = None
    for method_name in _PROVIDER_FETCH_METHODS[resolved_dataset]:
        candidate = getattr(resolved_provider, method_name, None)
        if callable(candidate):
            fetch = candidate
            break
    if fetch is None:
        raise IngestionError(
            f"provider does not implement a fetch method for {resolved_dataset}"
        )

    try:
        fetched = fetch(**dict(fetch_kwargs or {}))
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError(
            f"provider fetch failed for {resolved_dataset}: {exc}"
        ) from exc

    resolved_source = (
        source
        or getattr(resolved_provider, "source_name", None)
        or resolved_provider.__class__.__name__
    )
    return ingest_records(
        store,
        resolved_dataset,
        fetched,
        status=status,
        source=resolved_source,
        observed_at_utc=observed_at_utc,
        recorded_at_utc=recorded_at_utc,
        run_id=run_id,
        run_metadata=run_metadata,
        bulk=bulk,
    )


def ingest_provider(
    store: RevisionStore | str | Path | None,
    provider: Any,
    datasets: Sequence[str] | None = None,
    *,
    status: RevisionStatus | str = RevisionStatus.OBSERVED,
    source: str | None = None,
    fetch_kwargs: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
) -> dict[str, IngestionResult]:
    """Fetch and ingest one or more datasets from a provider."""
    resolved_datasets = list(datasets or _PROVIDER_FETCH_METHODS)
    results: dict[str, IngestionResult] = {}
    for dataset in resolved_datasets:
        results[normalize_dataset(dataset)] = ingest_dataset(
            store,
            provider,
            dataset,
            status=status,
            source=source,
            fetch_kwargs=fetch_kwargs,
            run_metadata=run_metadata,
        )
    return results


ingest = ingest_records
ingest_normalized_records = ingest_records
ingest_provider_records = ingest_dataset


__all__ = [
    "IngestResult",
    "IngestionError",
    "IngestionResult",
    "ingest",
    "ingest_dataset",
    "ingest_normalized_records",
    "ingest_provider",
    "ingest_provider_records",
    "ingest_records",
]
