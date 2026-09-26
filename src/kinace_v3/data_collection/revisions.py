from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import (
    BaseRecord,
    DataCollectionModelError,
    RevisionStatus,
    canonical_json_bytes,
    natural_key_for,
    normalize_dataset,
    normalize_record,
)


REVISION_SCHEMA_VERSION = 1


class RevisionError(ValueError):
    """Raised when a data revision or its identity is invalid."""


def normalize_status(status: RevisionStatus | str) -> RevisionStatus:
    if isinstance(status, RevisionStatus):
        return status
    if not isinstance(status, str):
        raise RevisionError("status must be a RevisionStatus or status string")
    try:
        return RevisionStatus(status.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in RevisionStatus)
        raise RevisionError(f"unsupported revision status {status!r}; expected one of {allowed}") from exc


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RevisionError(f"{field} must be a non-empty string")
    return value


def _optional_revision_id(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _required_text(value, field)


def _natural_key(value: Mapping[str, Any] | None, field: str = "natural_key") -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise RevisionError(f"{field} must be a non-empty object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise RevisionError(f"{field} keys must be non-empty strings")
        result[key] = item
    try:
        canonical_json_bytes(result)
    except DataCollectionModelError as exc:
        raise RevisionError(f"{field} is not canonical JSON: {exc}") from exc
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: str | None, field: str) -> str:
    if value is None:
        return _utc_now()
    return _required_text(value, field)


def canonical_revision_identity(
    *,
    dataset: str,
    natural_key: Mapping[str, Any],
    record: BaseRecord | Mapping[str, Any],
    status: RevisionStatus | str = RevisionStatus.OBSERVED,
    source: str = "manual",
    supersedes_revision_id: str | None = None,
    schema_version: int = REVISION_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Return the semantic content that determines a revision ID.

    Run IDs, observation timestamps, and record timestamps are intentionally
    excluded. Re-ingesting the same source data is therefore idempotent.
    """
    resolved_dataset = normalize_dataset(dataset)
    canonical_record = normalize_record(resolved_dataset, record)
    expected_key = natural_key_for(resolved_dataset, canonical_record)
    supplied_key = _natural_key(natural_key)
    if supplied_key != expected_key:
        raise RevisionError(
            f"natural_key does not match canonical record: {supplied_key!r} != {expected_key!r}"
        )
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        raise RevisionError("schema_version must be a positive integer")
    return {
        "dataset": resolved_dataset,
        "natural_key": expected_key,
        "record": canonical_record,
        "schema_version": schema_version,
        "source": _required_text(source, "source"),
        "status": normalize_status(status).value,
        "supersedes_revision_id": _optional_revision_id(
            supersedes_revision_id, "supersedes_revision_id"
        ),
    }


def revision_id_for(
    *,
    dataset: str,
    natural_key: Mapping[str, Any],
    record: BaseRecord | Mapping[str, Any],
    status: RevisionStatus | str = RevisionStatus.OBSERVED,
    source: str = "manual",
    supersedes_revision_id: str | None = None,
    schema_version: int = REVISION_SCHEMA_VERSION,
) -> str:
    """Compute a deterministic SHA256 revision ID from canonical content."""
    identity = canonical_revision_identity(
        dataset=dataset,
        natural_key=natural_key,
        record=record,
        status=status,
        source=source,
        supersedes_revision_id=supersedes_revision_id,
        schema_version=schema_version,
    )
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


compute_revision_id = revision_id_for
sha256_revision_id = revision_id_for


@dataclass(frozen=True)
class Revision:
    """An immutable canonical record revision."""

    dataset: str
    natural_key: Mapping[str, Any]
    record: BaseRecord | Mapping[str, Any]
    status: RevisionStatus | str = RevisionStatus.OBSERVED
    source: str = "manual"
    observed_at_utc: str | None = None
    recorded_at_utc: str | None = None
    ingestion_run_id: str = ""
    supersedes_revision_id: str | None = None
    revision_id: str | None = None
    schema_version: int = REVISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        resolved_dataset = normalize_dataset(self.dataset)
        canonical_record = normalize_record(resolved_dataset, self.record)
        expected_key = natural_key_for(resolved_dataset, canonical_record)
        supplied_key = _natural_key(self.natural_key)
        if supplied_key != expected_key:
            raise RevisionError(
                f"natural_key does not match canonical record: {supplied_key!r} != {expected_key!r}"
            )
        status = normalize_status(self.status)
        source = _required_text(self.source, "source")
        observed_at = _timestamp(self.observed_at_utc, "observed_at_utc")
        recorded_at = _timestamp(self.recorded_at_utc, "recorded_at_utc")
        supersedes = _optional_revision_id(
            self.supersedes_revision_id, "supersedes_revision_id"
        )
        expected_id = revision_id_for(
            dataset=resolved_dataset,
            natural_key=expected_key,
            record=canonical_record,
            status=status,
            source=source,
            supersedes_revision_id=supersedes,
            schema_version=self.schema_version,
        )
        supplied_id = _optional_revision_id(self.revision_id, "revision_id")
        if supplied_id is not None and supplied_id != expected_id:
            raise RevisionError(
                f"revision_id does not match canonical content: {supplied_id} != {expected_id}"
            )
        object.__setattr__(self, "dataset", resolved_dataset)
        object.__setattr__(self, "natural_key", expected_key)
        object.__setattr__(self, "record", canonical_record)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "observed_at_utc", observed_at)
        object.__setattr__(self, "recorded_at_utc", recorded_at)
        object.__setattr__(self, "ingestion_run_id", str(self.ingestion_run_id or ""))
        object.__setattr__(self, "supersedes_revision_id", supersedes)
        object.__setattr__(self, "revision_id", expected_id)

    @property
    def payload(self) -> dict[str, Any]:
        return dict(self.record)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "schema_version": self.schema_version,
            "dataset": self.dataset,
            "natural_key": dict(self.natural_key),
            "status": self.status.value,
            "record": dict(self.record),
            "source": self.source,
            "observed_at_utc": self.observed_at_utc,
            "recorded_at_utc": self.recorded_at_utc,
            "ingestion_run_id": self.ingestion_run_id,
            "supersedes_revision_id": self.supersedes_revision_id,
        }

    as_dict = to_dict


DataRevision = Revision


def make_revision(
    record: BaseRecord | Mapping[str, Any],
    *,
    dataset: str | None = None,
    natural_key: Mapping[str, Any] | None = None,
    status: RevisionStatus | str = RevisionStatus.OBSERVED,
    source: str = "manual",
    observed_at_utc: str | None = None,
    recorded_at_utc: str | None = None,
    ingestion_run_id: str = "",
    supersedes_revision_id: str | None = None,
    revision_id: str | None = None,
    schema_version: int = REVISION_SCHEMA_VERSION,
) -> Revision:
    if dataset is None:
        if not isinstance(record, BaseRecord):
            raise RevisionError("dataset is required when record is not a canonical model")
        dataset = record.dataset
    resolved_dataset = normalize_dataset(dataset)
    key = natural_key_for(resolved_dataset, record) if natural_key is None else natural_key
    return Revision(
        dataset=resolved_dataset,
        natural_key=key,
        record=record,
        status=status,
        source=source,
        observed_at_utc=observed_at_utc,
        recorded_at_utc=recorded_at_utc,
        ingestion_run_id=ingestion_run_id,
        supersedes_revision_id=supersedes_revision_id,
        revision_id=revision_id,
        schema_version=schema_version,
    )


create_revision = make_revision


__all__ = [
    "DataRevision",
    "REVISION_SCHEMA_VERSION",
    "Revision",
    "RevisionError",
    "canonical_revision_identity",
    "compute_revision_id",
    "create_revision",
    "make_revision",
    "normalize_status",
    "revision_id_for",
    "sha256_revision_id",
]
