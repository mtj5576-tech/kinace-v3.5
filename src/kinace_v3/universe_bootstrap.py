"""Authoritative historical universe bootstrap for stock metadata and index membership."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .data_collection import (
    RevisionStore,
    TushareProProvider,
    ingest_dataset,
    ingest_records,
)
from .feature_snapshot import canonical_json_bytes


DEFAULT_INDEX_CODES = ("000300.SH", "000905.SH", "000852.SH", "000016.SH")
DEFAULT_UNIVERSE_REPORT_DIR = PROJECT_ROOT / "reports" / "universe"


class UniverseBootstrapError(ValueError):
    """Raised when historical universe data cannot be bootstrapped."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def run_universe_bootstrap(
    database: str | Path | None,
    *,
    index_codes: Sequence[str] = DEFAULT_INDEX_CODES,
    list_statuses: Sequence[str] = ("L", "D", "P"),
    token: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_industry_history: bool = True,
    industry_level: str = "L1",
    industry_src: str = "SW2021",
    provider: TushareProProvider | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    resolved_token = token or os.environ.get("TUSHARE_TOKEN")
    if provider is None:
        if not resolved_token:
            raise UniverseBootstrapError(
                "TUSHARE_TOKEN is required for authoritative list/delist and index history"
            )
        provider = TushareProProvider(token=resolved_token)
    store = RevisionStore(target)
    results: dict[str, Any] = {
        "stock_metadata": [],
        "sector_membership": [],
        "industry_membership": [],
    }
    for status in list_statuses:
        result = ingest_dataset(
            store,
            "stock_metadata",
            provider,
            status="observed",
            source="tushare_stock_basic",
            fetch_kwargs={
                "list_status": status,
                "fields": (
                    "ts_code,symbol,name,area,industry,market,"
                    "list_status,list_date,delist_date,is_hs"
                ),
            },
            bulk=True,
            run_metadata={
                "operation": "universe_bootstrap",
                "dataset": "stock_metadata",
                "list_status": status,
            },
        )
        results["stock_metadata"].append(result.to_dict())
    for index_code in index_codes:
        result = ingest_dataset(
            store,
            "sector_membership",
            provider,
            status="observed",
            source="tushare_index_member",
            fetch_kwargs={
                "index_code": index_code,
                "start_date": start_date,
                "end_date": end_date,
            },
            bulk=True,
            run_metadata={
                "operation": "universe_bootstrap",
                "dataset": "sector_membership",
                "index_code": index_code,
            },
        )
        results["sector_membership"].append(result.to_dict())
    industry_classifications: list[Mapping[str, Any]] = []
    if include_industry_history:
        classify = getattr(provider, "index_classify", None)
        industry_member = getattr(provider, "industry_member", None)
        if not callable(classify) or not callable(industry_member):
            raise UniverseBootstrapError(
                "provider does not support historical industry membership"
            )
        industry_classifications = list(
            classify(
                level=industry_level,
                src=industry_src,
                fields="index_code,industry_name,level,src",
            )
        )
        for classification in industry_classifications:
            index_code = str(classification.get("index_code") or "").strip()
            industry_name = str(classification.get("industry_name") or "").strip()
            if not index_code or not industry_name:
                continue
            records = industry_member(
                index_code,
                industry_name=industry_name,
                fields="index_code,con_code,in_date,out_date,is_new",
            )
            result = ingest_records(
                store,
                "sector_membership",
                records,
                status="observed",
                source="tushare_index_member",
                bulk=True,
                run_metadata={
                    "operation": "universe_bootstrap",
                    "dataset": "sector_membership",
                    "index_code": index_code,
                    "industry_name": industry_name,
                    "industry_src": industry_src,
                },
            )
            results["industry_membership"].append(
                {
                    "index_code": index_code,
                    "industry_name": industry_name,
                    **result.to_dict(),
                }
            )
    payload = {
        "artifact_type": "kinace_v3_universe_bootstrap",
        "database": str(target),
        "index_codes": list(index_codes),
        "list_statuses": list(list_statuses),
        "date_range": {"start": start_date, "end": end_date},
        "industry_classification": {
            "enabled": include_industry_history,
            "level": industry_level,
            "source": industry_src,
            "classification_count": len(industry_classifications),
        },
        "results": results,
        "completed_at_utc": _utc_now(),
        "production_ready": False,
        "production_changed": False,
    }
    target_path = (
        Path(output_path).resolve(strict=False)
        if output_path is not None
        else DEFAULT_UNIVERSE_REPORT_DIR / "universe-bootstrap.json"
    )
    written = _write_json(payload, target_path)
    payload["output_path"] = str(written)
    return payload


__all__ = [
    "DEFAULT_INDEX_CODES",
    "DEFAULT_UNIVERSE_REPORT_DIR",
    "UniverseBootstrapError",
    "run_universe_bootstrap",
]
