"""Canonical price-source rebuild and snapshot regeneration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .data_collection import (
    RevisionStatus,
    RevisionStore,
    ingest_records,
)
from .feature_snapshot import canonical_json_bytes, sha256_file
from .history_features import DEFAULT_MODEL_CONFIG, build_history_snapshots
from .pit_universe import PitUniverseError, resolve_pit_policy


SCHEMA_VERSION = 1
DEFAULT_CANONICAL_SOURCE = "kinace_v2_1_history"
DEFAULT_CANONICAL_SNAPSHOT_DIR = Path(r"D:\kinacesqlv3.2\snapshots_canonical")
DEFAULT_REBUILD_REPORT_DIR = PROJECT_ROOT / "reports" / "rebuild"
MARKET_DATASETS = (
    "daily_bars",
    "daily_basic_metrics",
    "security_state",
    "forward_labels",
)


class DataRebuildError(ValueError):
    """Raised when the canonical market rebuild cannot run safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("record")
    return value if isinstance(value, Mapping) else row


def _status(row: Mapping[str, Any]) -> str:
    return str(row.get("effective_status") or row.get("status") or "")


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def plan_canonical_rebuild(
    database: str | Path,
    *,
    canonical_source: str = DEFAULT_CANONICAL_SOURCE,
) -> dict[str, Any]:
    target = validate_database_path(database)
    store = RevisionStore(target)
    plan: dict[str, list[dict[str, Any]]] = {dataset: [] for dataset in MARKET_DATASETS}
    for dataset in MARKET_DATASETS:
        rows = store.current_records(dataset)
        canonical_codes = {
            str(_record(row).get("ts_code") or "")
            for row in rows
            if str(row.get("source") or "") == canonical_source
            and _status(row)
            in {RevisionStatus.OBSERVED.value, RevisionStatus.BACKFILLED_RELIABLE.value}
        }
        for row in rows:
            source = str(row.get("source") or "")
            record = dict(_record(row))
            code = str(record.get("ts_code") or "")
            if (
                source != canonical_source
                and code in canonical_codes
                and _status(row)
                in {
                    RevisionStatus.OBSERVED.value,
                    RevisionStatus.BACKFILLED_RELIABLE.value,
                }
            ):
                plan[dataset].append(record)
    return {
        "artifact_type": "kinace_v3_canonical_price_rebuild_plan",
        "schema_version": SCHEMA_VERSION,
        "database": str(target),
        "canonical_source": canonical_source,
        "invalidate_counts": {
            dataset: len(records) for dataset, records in plan.items()
        },
        "invalidate_records": plan,
        "created_at_utc": _utc_now(),
    }


def apply_canonical_rebuild(
    database: str | Path,
    *,
    canonical_source: str = DEFAULT_CANONICAL_SOURCE,
    apply: bool = False,
    snapshot_dir: str | Path | None = None,
    rebuild_snapshots: bool = False,
    model_config: Mapping[str, Any] | None = None,
    pit_policy: Mapping[str, Any] | None = None,
    pit_policy_path: str | Path | None = None,
) -> dict[str, Any]:
    target = validate_database_path(database)
    plan = plan_canonical_rebuild(target, canonical_source=canonical_source)
    store = RevisionStore(target)
    applied: dict[str, Any] = {}
    if apply:
        for dataset in MARKET_DATASETS:
            records = plan["invalidate_records"][dataset]
            if not records:
                applied[dataset] = {
                    "inserted_count": 0,
                    "skipped_count": 0,
                }
                continue
            result = ingest_records(
                store,
                dataset,
                records,
                status=RevisionStatus.INVALID,
                source="canonical_price_rebuild",
                bulk=True,
                run_metadata={
                    "operation": "canonical_price_rebuild",
                    "canonical_source": canonical_source,
                    "policy": "invalidate_noncanonical_nonoverlap_prices",
                },
            )
            applied[dataset] = result.to_dict()
    snapshots: dict[str, Any] | None = None
    policy_resolution: dict[str, Any] | None = None
    if rebuild_snapshots:
        if not apply:
            raise DataRebuildError(
                "snapshots cannot be rebuilt until invalid revisions are applied"
            )
        try:
            policy_resolution = resolve_pit_policy(
                pit_policy,
                policy_path=pit_policy_path,
            )
        except PitUniverseError as exc:
            raise DataRebuildError(str(exc)) from exc
        resolved_snapshot_dir = (
            Path(snapshot_dir).resolve(strict=False)
            if snapshot_dir is not None
            else DEFAULT_CANONICAL_SNAPSHOT_DIR
        )
        snapshots = build_history_snapshots(
            store,
            database=target,
            output_dir=resolved_snapshot_dir,
            model_config=model_config or DEFAULT_MODEL_CONFIG,
            pit_policy=policy_resolution["policy"],
            pit_policy_path=policy_resolution["policy_path"],
            snapshot_mode=policy_resolution["mode"],
        )
    result = {
        **plan,
        "apply": bool(apply),
        "applied": applied,
        "snapshots": snapshots,
        "completed_at_utc": _utc_now(),
    }
    if policy_resolution is not None:
        result.update(
            {
                "pit_policy": policy_resolution["policy"],
                "pit_policy_hash": policy_resolution["pit_policy_hash"],
                "industry_source": policy_resolution["industry_source"],
                "industry_coverage": (
                    snapshots.get("industry_coverage") if snapshots else None
                ),
                "pit_ready": bool(snapshots and snapshots.get("pit_ready") is True),
            }
        )
    return result


def write_rebuild_report(
    payload: Mapping[str, Any],
    output_path: str | Path,
) -> Path:
    written = _write_json(payload, output_path)
    return written


__all__ = [
    "DEFAULT_CANONICAL_SNAPSHOT_DIR",
    "DEFAULT_CANONICAL_SOURCE",
    "DEFAULT_REBUILD_REPORT_DIR",
    "DataRebuildError",
    "MARKET_DATASETS",
    "apply_canonical_rebuild",
    "plan_canonical_rebuild",
    "write_rebuild_report",
]
