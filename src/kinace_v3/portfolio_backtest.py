"""Build and run one continuous portfolio backtest across historical snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT
from .data_collection.models import RevisionStatus
from .data_collection.store import RevisionStore
from .experiments import initialize_experiments_database, record_experiment, record_replay
from .feature_snapshot import (
    canonical_json_bytes,
    load_feature_snapshot_artifact,
    sha256_bytes,
    sha256_file,
    validate_model_config,
)
from .score_replay import (
    apply_score_persistence,
    normalize_score_policy,
    recompute_candidate_scores,
    write_replay_artifact,
)
from .research_feature_transform import FeatureTransformState
from .price_basis import (
    ADJUSTED_PRICE_BASIS,
    RAW_PRICE_BASIS,
    RESEARCH_PRICE_BASES,
    TOTAL_RETURN_PRICE_BASIS,
    PriceBasisError,
    derive_adjusted_bars,
    normalize_price_basis,
    tag_price_series,
    unpack_market_views,
)
from .trial_backtest import (
    normalize_backtest_config,
    normalize_market_bars,
    normalize_market_views,
    run_trial_backtest,
)


SCHEMA_VERSION = 1
ALLOWED_REVISION_STATUSES = {
    RevisionStatus.OBSERVED.value,
    RevisionStatus.BACKFILLED_RELIABLE.value,
}


class PortfolioBacktestError(ValueError):
    """Raised when a full-history continuous portfolio backtest cannot run."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PortfolioBacktestError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioBacktestError(f"{field} must be a non-empty string")
    return value.strip()


def _normal_date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        raise PortfolioBacktestError(f"{field} must be YYYY-MM-DD")
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError as exc:
        raise PortfolioBacktestError(f"{field} must be YYYY-MM-DD") from exc


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def _snapshot_manifest_path(snapshot_dir: Path) -> Path:
    return snapshot_dir / "manifest.json"


def validate_snapshot_manifest_for_research(
    payload: Mapping[str, Any],
    *,
    source: str = "snapshot manifest",
) -> None:
    """Reject snapshot sets that are not explicitly safe for historical research."""
    research_basis = payload.get("price_basis")
    if research_basis is not None:
        try:
            resolved_research_basis = normalize_price_basis(
                research_basis,
                field=f"{source}.price_basis",
            )
        except PriceBasisError as exc:
            raise PortfolioBacktestError(str(exc)) from exc
        if resolved_research_basis not in RESEARCH_PRICE_BASES:
            raise PortfolioBacktestError(
                f"{source} research price_basis must be adjusted or total_return"
            )
    execution_basis = payload.get("execution_price_basis")
    if execution_basis is not None:
        try:
            resolved_execution_basis = normalize_price_basis(
                execution_basis,
                field=f"{source}.execution_price_basis",
            )
        except PriceBasisError as exc:
            raise PortfolioBacktestError(str(exc)) from exc
        if resolved_execution_basis != RAW_PRICE_BASIS:
            raise PortfolioBacktestError(f"{source} execution price_basis must be raw")
    if (
        payload.get("snapshot_mode") == "live_current"
        or payload.get("pit_ready") is False
        or payload.get("industry_lookahead_safe") is False
    ):
        raise PortfolioBacktestError(
            f"{source} is not PIT-ready or is marked industry_lookahead_safe=false"
        )
    if payload.get("artifact_type") == "kinace_v3_history_feature_build":
        required = (
            "pit_policy_hash",
            "industry_source",
            "industry_coverage",
            "price_basis",
            "execution_price_basis",
        )
        missing = [field for field in required if payload.get(field) is None]
        if missing or payload.get("pit_ready") is not True:
            raise PortfolioBacktestError(
                f"{source} is missing required PIT audit fields: "
                + ", ".join(missing or ["pit_ready"])
            )
        if payload.get("industry_lookahead_safe") is not True:
            raise PortfolioBacktestError(
                f"{source} is not explicitly marked industry_lookahead_safe=true"
            )


def validate_snapshot_directory_for_research(snapshot_dir: str | Path) -> None:
    manifest_path = _snapshot_manifest_path(Path(snapshot_dir).resolve(strict=False))
    if not manifest_path.is_file():
        return
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortfolioBacktestError(
            f"cannot read snapshot manifest: {manifest_path}: {exc}"
        ) from exc
    validate_snapshot_manifest_for_research(payload, source=str(manifest_path))


def _snapshot_rows_from_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortfolioBacktestError(f"cannot read snapshot manifest: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise PortfolioBacktestError("snapshot manifest root must be an object")
    validate_snapshot_manifest_for_research(payload, source=str(manifest_path))
    rows = payload.get("snapshots")
    if not isinstance(rows, list):
        raise PortfolioBacktestError("snapshot manifest must contain a snapshots list")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        item = _mapping(row, f"manifest.snapshots[{index}]")
        decision_date = _normal_date(
            item.get("decision_date"), f"manifest.snapshots[{index}].decision_date"
        )
        result.append({"decision_date": decision_date, "manifest_entry": dict(item)})
    return result


def _snapshot_rows_from_directory(snapshot_dir: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(snapshot_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            payload = load_feature_snapshot_artifact(path)
        except Exception as exc:
            raise PortfolioBacktestError(f"cannot load snapshot {path}: {exc}") from exc
        decision_date = _normal_date(payload.get("decision_date"), f"{path}.decision_date")
        result.append(
            {
                "decision_date": decision_date,
                "path": str(path.resolve(strict=False)),
                "snapshot_id": sha256_file(path),
            }
        )
    return result


def discover_snapshot_dates(
    snapshot_dir: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[str]:
    """Return ordered decision dates without loading reserved snapshot payloads."""
    root = Path(snapshot_dir).resolve(strict=False)
    if not root.is_dir():
        raise PortfolioBacktestError(f"snapshot directory does not exist: {root}")
    start = _normal_date(start_date, "start_date") if start_date else None
    end = _normal_date(end_date, "end_date") if end_date else None
    if start and end and start > end:
        raise PortfolioBacktestError("start_date must not be later than end_date")

    manifest_path = _snapshot_manifest_path(root)
    if manifest_path.is_file():
        rows = _snapshot_rows_from_manifest(manifest_path)
        decision_dates = [str(row["decision_date"]) for row in rows]
    else:
        decision_dates = [
            _normal_date(path.stem, f"{path}.decision_date")
            for path in sorted(root.glob("*.json"))
            if path.name != "manifest.json"
        ]
    selected: list[str] = []
    seen_dates: set[str] = set()
    for decision_date in decision_dates:
        if start and decision_date < start:
            continue
        if end and decision_date > end:
            continue
        if decision_date in seen_dates:
            raise PortfolioBacktestError(
                f"duplicate snapshot decision date: {decision_date}"
            )
        seen_dates.add(decision_date)
        selected.append(decision_date)
    if not selected:
        raise PortfolioBacktestError("no feature snapshots match the requested date range")
    return sorted(selected)


def discover_snapshots(
    snapshot_dir: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Return validated snapshot records ordered by decision date."""
    root = Path(snapshot_dir).resolve(strict=False)
    if not root.is_dir():
        raise PortfolioBacktestError(f"snapshot directory does not exist: {root}")
    start = _normal_date(start_date, "start_date") if start_date else None
    end = _normal_date(end_date, "end_date") if end_date else None
    if start and end and start > end:
        raise PortfolioBacktestError("start_date must not be later than end_date")

    manifest_path = _snapshot_manifest_path(root)
    rows = (
        _snapshot_rows_from_manifest(manifest_path)
        if manifest_path.is_file()
        else _snapshot_rows_from_directory(root)
    )
    selected: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for row in rows:
        decision_date = str(row["decision_date"])
        if start and decision_date < start:
            continue
        if end and decision_date > end:
            continue
        if decision_date in seen_dates:
            raise PortfolioBacktestError(f"duplicate snapshot decision date: {decision_date}")
        seen_dates.add(decision_date)
        entry = dict(row.get("manifest_entry") or {})
        path_value = entry.get("artifact_path") or row.get("path")
        if not path_value:
            path_value = root / f"{decision_date}.json"
        path = Path(path_value).resolve(strict=False)
        if not path.is_file():
            raise PortfolioBacktestError(f"snapshot artifact is missing: {path}")
        payload = load_feature_snapshot_artifact(path)
        validate_snapshot_manifest_for_research(payload, source=str(path))
        actual_date = _normal_date(payload.get("decision_date"), f"{path}.decision_date")
        if actual_date != decision_date:
            raise PortfolioBacktestError(
                f"snapshot date mismatch: manifest={decision_date}, artifact={actual_date}"
            )
        snapshot_hash = sha256_file(path)
        selected.append(
            {
                "decision_date": decision_date,
                "path": str(path),
                "snapshot_id": snapshot_hash,
                "artifact_sha256": snapshot_hash,
            }
        )
    if not selected:
        raise PortfolioBacktestError("no feature snapshots match the requested date range")
    selected.sort(key=lambda item: item["decision_date"])
    return selected


def build_snapshot_set_manifest(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    output_dir: str | Path,
    track: str | None = None,
) -> dict[str, Any]:
    """Write a content-addressed manifest describing the exact snapshot set."""
    if not snapshots:
        raise PortfolioBacktestError("at least one snapshot is required")
    rows = [
        {
            "decision_date": _normal_date(item.get("decision_date"), "snapshot.decision_date"),
            "snapshot_id": _required_text(item.get("snapshot_id"), "snapshot.snapshot_id"),
            "artifact_sha256": _required_text(
                item.get("artifact_sha256"), "snapshot.artifact_sha256"
            ),
            "artifact_path": str(Path(item.get("path")).resolve(strict=False)),
        }
        for item in snapshots
    ]
    from .synthetic_sandbox import active_track

    resolved_track = track or active_track(
        output_dir,
        *(item.get("path") for item in snapshots),
    )
    payload = {
        "artifact_type": "kinace_v3_snapshot_set_manifest",
        "schema_version": SCHEMA_VERSION,
        "track": resolved_track,
        "decision_date_start": rows[0]["decision_date"],
        "decision_date_end": rows[-1]["decision_date"],
        "snapshot_count": len(rows),
        "snapshots": rows,
    }
    digest = sha256_bytes(canonical_json_bytes(payload, trailing_newline=False))
    target = Path(output_dir).resolve(strict=False) / f"snapshot-set-{digest}.json"
    written = _write_json(payload, target)
    return {
        **payload,
        "snapshot_set_id": digest,
        "artifact_path": str(written),
        "artifact_sha256": sha256_file(written),
    }


def _record_status(record: Mapping[str, Any]) -> str:
    return str(record.get("effective_status") or record.get("status") or "")


def _record_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("record")
    return value if isinstance(value, Mapping) else row


def _adjustment_factor_rows(store: RevisionStore | str | Path) -> list[dict[str, Any]]:
    from .history_features import _iter_current_dataset_rows, _record

    if os.environ.get("KINACE_TUSHARE_PRICE_BASIS", "raw").strip().lower() != "raw":
        return []
    resolved_store = store if isinstance(store, RevisionStore) else RevisionStore(store)
    try:
        return [
            dict(_record(row))
            for row in _iter_current_dataset_rows(
                resolved_store.database,
                "adjustment_factors",
            )
            if _record_status(row) in ALLOWED_REVISION_STATUSES
        ]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


def load_market_bars(
    store: RevisionStore | str | Path,
    *,
    price_basis: str = RAW_PRICE_BASIS,
) -> dict[str, list[dict[str, Any]]]:
    """Load one explicitly tagged price view from canonical market revisions."""
    from .history_features import _iter_current_dataset_rows, _record

    try:
        resolved_price_basis = normalize_price_basis(price_basis)
    except PriceBasisError as exc:
        raise PortfolioBacktestError(str(exc)) from exc
    resolved_store = store if isinstance(store, RevisionStore) else RevisionStore(store)
    daily: dict[tuple[str, str], dict[str, Any]] = {}
    states: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _iter_current_dataset_rows(resolved_store.database, "daily_bars"):
        status = _record_status(row)
        if status not in ALLOWED_REVISION_STATUSES:
            continue
        record = _record(row)
        code = str(record.get("ts_code") or "")
        trade_date = str(record.get("trade_date") or "")
        if not code or not trade_date:
            continue
        key = (code, trade_date)
        daily[key] = dict(record)
    for row in _iter_current_dataset_rows(resolved_store.database, "security_state"):
        status = _record_status(row)
        if status not in ALLOWED_REVISION_STATUSES:
            continue
        record = _record(row)
        code = str(record.get("ts_code") or "")
        trade_date = str(record.get("trade_date") or "")
        if not code or not trade_date:
            continue
        states[(code, trade_date)] = dict(record)
    if not daily:
        raise PortfolioBacktestError("no eligible daily bars are available")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for (code, trade_date), record in sorted(daily.items()):
        state = states.get((code, trade_date), {})
        bar = {
            "code": code,
            "trade_date": trade_date,
            "open": record.get("open"),
            "high": record.get("high"),
            "low": record.get("low"),
            "close": record.get("close"),
            "is_suspended": bool(state.get("is_suspended", False)),
            "is_limit_up": bool(state.get("is_limit_up", False)),
            "is_limit_down": bool(state.get("is_limit_down", False)),
        }
        volume = record.get("vol", record.get("volume"))
        if volume is not None:
            bar["volume"] = volume
        grouped.setdefault(code, []).append(bar)
    if resolved_price_basis == RAW_PRICE_BASIS:
        return normalize_market_bars(
            tag_price_series(grouped, RAW_PRICE_BASIS),
            price_basis=RAW_PRICE_BASIS,
        )
    if resolved_price_basis not in RESEARCH_PRICE_BASES:
        raise PortfolioBacktestError(
            "market loaders require raw, adjusted, or total_return price_basis"
        )

    factor_rows = _adjustment_factor_rows(resolved_store)
    try:
        return normalize_market_bars(
            derive_adjusted_bars(
                normalize_market_bars(grouped),
                factor_rows,
                output_basis=resolved_price_basis,
            ),
            price_basis=resolved_price_basis,
        )
    except (PriceBasisError, ValueError) as exc:
        raise PortfolioBacktestError(str(exc)) from exc


def load_market_bar_views(
    store: RevisionStore | str | Path,
    *,
    research_price_basis: str = TOTAL_RETURN_PRICE_BASIS,
) -> dict[str, dict[str, Any]]:
    """Load separate raw execution and adjusted research price series."""
    try:
        resolved_research_basis = normalize_price_basis(research_price_basis)
    except PriceBasisError as exc:
        raise PortfolioBacktestError(str(exc)) from exc
    if resolved_research_basis not in RESEARCH_PRICE_BASES:
        raise PortfolioBacktestError(
            "research market data requires adjusted or total_return price_basis"
        )
    return {
        "execution": tag_price_series(
            load_market_bars(store, price_basis=RAW_PRICE_BASIS),
            RAW_PRICE_BASIS,
        ),
        "research": tag_price_series(
            load_market_bars(store, price_basis=resolved_research_basis),
            resolved_research_basis,
        ),
    }


def build_portfolio_replay(
    snapshots: Sequence[Mapping[str, Any]],
    candidate_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine point-in-time snapshot scores into one continuous replay artifact."""
    candidate = validate_model_config(candidate_config)
    transform_state = FeatureTransformState(candidate.get("feature_transform"))
    score_policy = normalize_score_policy(candidate.get("score_policy"))
    all_scores: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for item in sorted(snapshots, key=lambda value: str(value.get("decision_date") or "")):
        path = Path(item["path"]).resolve(strict=False)
        payload = load_feature_snapshot_artifact(path)
        decision_date = str(payload.get("decision_date") or "")
        overrides = transform_state.apply(decision_date, payload)
        scores = recompute_candidate_scores(
            payload,
            candidate,
            raw_input_overrides=overrides,
        )
        all_scores.extend(scores)
        source_rows.append(
            {
                "decision_date": str(payload.get("decision_date") or ""),
                "snapshot_id": sha256_file(path),
                "artifact_path": str(path),
            }
        )
    if not all_scores:
        raise PortfolioBacktestError("combined snapshots produced no candidate scores")
    all_scores = apply_score_persistence(all_scores, score_policy)
    all_scores.sort(key=lambda item: (item["decision_date"], item["code"]))
    candidate_id = hashlib.sha256(
        canonical_json_bytes(candidate, trailing_newline=False)
    ).hexdigest()
    source_identity = sha256_bytes(
        canonical_json_bytes(
            {"kind": "continuous_portfolio", "snapshots": source_rows},
            trailing_newline=False,
        )
    )
    return {
        "artifact_type": "kinace_v3_score_replay",
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "candidate_config": candidate,
        "source_snapshot": {
            "snapshot_id": source_identity,
            "artifact_sha256": source_identity,
            "mode": "continuous_portfolio",
            "snapshot_count": len(source_rows),
            "snapshots": source_rows,
        },
        "data_gate": {
            "status": "continuous_portfolio",
            "ready": False,
            "parity_claimed": False,
            "snapshot_count": len(source_rows),
        },
        "replay_semantics": {
            "formula": "direction_adjusted_weighted_z_scores_minus_penalties_logistic",
            "feature_standardization_source": "candidate_config_recomputed_from_raw_inputs",
            "feature_transform": dict(candidate.get("feature_transform") or {}),
            "score_policy": score_policy,
            "stored_z_score_inputs_reused": False,
            "stored_recommendation_scores_reused": False,
            "real_data_parity_claimed": False,
            "mode": "continuous_portfolio",
        },
        "scores": all_scores,
    }


def run_portfolio_backtest(
    database: str | Path | None,
    candidate_config: Mapping[str, Any],
    snapshot_dir: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    bars: Any | None = None,
    research_bars: Any | None = None,
    price_basis: str | None = None,
    research_price_basis: str | None = None,
    config: Mapping[str, Any] | None = None,
    output_path: str | Path | None = None,
    run_id: str | None = None,
    code_version: str = "kinace-v3",
) -> dict[str, Any]:
    """Run a single-account continuous backtest over a full historical snapshot set."""
    settings = normalize_backtest_config(config)
    candidate = validate_model_config(candidate_config)
    snapshots = discover_snapshots(
        snapshot_dir,
        start_date=start_date,
        end_date=end_date,
    )
    manifest_dir = (
        Path(output_path).resolve(strict=False).parent
        if output_path
        else PROJECT_ROOT / "reports"
    )
    snapshot_manifest = build_snapshot_set_manifest(snapshots, output_dir=manifest_dir)
    replay_payload = build_portfolio_replay(snapshots, candidate)
    replay_payload["source_snapshot"].update(
        {
            "artifact_path": snapshot_manifest["artifact_path"],
            "artifact_sha256": snapshot_manifest["artifact_sha256"],
            "snapshot_set_id": snapshot_manifest["snapshot_set_id"],
        }
    )
    replay_payload["replay_semantics"]["snapshot_set_id"] = snapshot_manifest[
        "snapshot_set_id"
    ]
    replay = write_replay_artifact(replay_payload)
    target_database = initialize_experiments_database(database)
    replay_id = record_replay(target_database, replay, code_version=code_version)
    experiment_id = record_experiment(
        target_database,
        code_version=code_version,
        data_snapshot_hash=snapshot_manifest["artifact_sha256"],
        search_space={
            "kind": "continuous_portfolio",
            "snapshot_count": len(snapshots),
            "start_date": snapshots[0]["decision_date"],
            "end_date": snapshots[-1]["decision_date"],
        },
        seed=0,
        objective_config={"metric": "continuous_portfolio"},
        candidate_id=replay_payload["candidate_id"],
        replay_id=replay_id,
        artifact_paths={
            "snapshot_set": snapshot_manifest["artifact_path"],
            "replay": replay["artifact_path"],
        },
    )
    if bars is None:
        market_views = load_market_bar_views(
            target_database,
            research_price_basis=research_price_basis or TOTAL_RETURN_PRICE_BASIS,
        )
    else:
        market_views = normalize_market_views(
            bars,
            research_bars,
            price_basis=price_basis,
            research_price_basis=research_price_basis,
        )
        combined_views = unpack_market_views(bars)
        has_research_view = research_bars is not None or (
            combined_views is not None and combined_views[2] is not None
        )
        if not has_research_view:
            resolved_research_basis = (
                normalize_price_basis(research_price_basis)
                if research_price_basis is not None
                else TOTAL_RETURN_PRICE_BASIS
            )
            factor_rows = _adjustment_factor_rows(target_database)
            if factor_rows:
                market_views["research"] = tag_price_series(
                    normalize_market_bars(
                        derive_adjusted_bars(
                            market_views["execution"]["bars"],
                            factor_rows,
                            output_basis=resolved_research_basis,
                        ),
                        price_basis=resolved_research_basis,
                        require_execution_flags=False,
                        require_volume=False,
                    ),
                    resolved_research_basis,
                )
    score_codes = {
        str(item.get("code") or "")
        for item in replay_payload.get("scores") or []
        if isinstance(item, Mapping) and str(item.get("code") or "")
    }
    filtered_views: dict[str, dict[str, Any]] = {}
    for view_name, view in market_views.items():
        filtered_bars = {
            code: [
                bar
                for bar in code_bars
                if (not score_codes or str(code) in score_codes)
                and (end_date is None or str(bar.get("trade_date") or "") <= end_date)
            ]
            for code, code_bars in view["bars"].items()
        }
        filtered_bars = {
            code: code_bars for code, code_bars in filtered_bars.items() if code_bars
        }
        if not filtered_bars:
            raise PortfolioBacktestError(
                f"no {view_name} market bars remain after filtering"
            )
        filtered_views[view_name] = tag_price_series(
            filtered_bars,
            view["price_basis"],
        )
    backtest = run_trial_backtest(
        target_database,
        replay_id,
        replay_payload["candidate_id"],
        filtered_views["execution"],
        research_bars=filtered_views["research"],
        experiment_id=experiment_id,
        config=settings,
        run_id=run_id,
        code_version=code_version,
    )
    from .synthetic_sandbox import active_track

    report = {
        "artifact_type": "kinace_v3_continuous_portfolio_backtest",
        "schema_version": SCHEMA_VERSION,
        "track": active_track(
            target_database,
            snapshot_dir,
            output_path,
            snapshot_manifest["artifact_path"],
        ),
        "mode": "continuous_portfolio",
        "database": str(target_database),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "snapshot_manifest": snapshot_manifest,
        "candidate_id": replay_payload["candidate_id"],
        "replay_id": replay_id,
        "experiment_id": experiment_id,
        "run_id": backtest["run_id"],
        "score_count": len(replay_payload["scores"]),
        "continuous": True,
        "date_range": {
            "start": backtest["start_date"],
            "end": backtest["end_date"],
        },
        "config": settings,
        "price_basis": {
            "execution": backtest["execution_price_basis"],
            "research": backtest["research_price_basis"],
        },
        "metrics": backtest["metrics"],
        "benchmark_proxy": {
            "label": "equal-weight market proxy",
            "is_official_index": False,
            "return_anomaly_policy": backtest["return_anomaly_policy"],
        },
        "equity_basis": backtest["metrics"]["equity_basis"],
        "mark_to_market_equity": backtest["mark_to_market_equity"],
        "liquidated_equity": backtest["liquidated_equity"],
        "liquidated_equity_detail": backtest["liquidated_equity_detail"],
        "closed_trade_stats": backtest["closed_trade_stats"],
        "open_positions": backtest["open_positions"],
        "equity": backtest["equity"],
        "trades": backtest["trades"],
        "artifact_path": backtest["artifact_path"],
        "artifact_sha256": backtest["artifact_sha256"],
        "backtest_evidence_id": backtest["backtest_evidence_id"],
        "completed_at_utc": _utc_now(),
        "production_ready": False,
        "production_changed": False,
    }
    target = (
        Path(output_path).resolve(strict=False)
        if output_path is not None
        else PROJECT_ROOT / "reports" / f"portfolio-{backtest['run_id']}.json"
    )
    written = _write_json(report, target)
    report["output_path"] = str(written)
    report["output_sha256"] = sha256_file(written)
    return report


__all__ = [
    "PortfolioBacktestError",
    "SCHEMA_VERSION",
    "build_portfolio_replay",
    "build_snapshot_set_manifest",
    "discover_snapshot_dates",
    "discover_snapshots",
    "load_market_bar_views",
    "load_market_bars",
    "run_portfolio_backtest",
    "validate_snapshot_directory_for_research",
    "validate_snapshot_manifest_for_research",
]
