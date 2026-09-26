"""Freeze and extend fixed-Holdout candidates with insufficient trades."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .holdout_access import (
    FORMAL_TRACK,
    bind_holdout_extension_access,
    resolve_holdout_access_registry_path,
)
from .optimization import score_portfolio_objective
from .portfolio_backtest import discover_snapshots, load_market_bars, run_portfolio_backtest
from .score_replay import validate_candidate_config
from .trial_backtest import normalize_backtest_config


SCHEMA_VERSION = 1
HOLDOUT_EXTENSION_POLICY_VERSION = "kinace-v3.3-holdout-min-trade-extension-v1"
DEFAULT_HOLDOUT_EXTENSION_POLICY: dict[str, Any] = {
    "version": HOLDOUT_EXTENSION_POLICY_VERSION,
    "scope": "fresh_fixed_holdout",
    "initial_holdout_sessions": 60,
    "min_trade_count": 30,
    "extension_step_sessions": 1,
    "max_total_sessions": 120,
    "trigger": "all_non_trade_holdout_gates_passed_and_trade_count_lt_30",
    "freeze_candidate_before_extension": True,
    "stop_on_trade_count_only": True,
    "stop_when_trade_count_reached": True,
    "reject_if_not_reached_by_max_sessions": True,
    "retroactive": False,
}
DEFAULT_EXTENSION_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "research_holdout_extension"
DEFAULT_EXTENSION_REPORT_DIR = PROJECT_ROOT / "reports" / "research_holdout_extension"
DEFAULT_HOLDOUT_EXTENSION_REGISTRY_PATH = (
    PROJECT_ROOT / "reports" / "holdout_extension_registry.json"
)


class ResearchHoldoutError(ValueError):
    """Raised when a fixed Holdout freeze or extension cannot be evaluated."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchHoldoutError(f"{field} must be an object")
    return value


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchHoldoutError(f"cannot load {field}: {exc}") from exc


def _write_json(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target)
    return target


def _write_text(text: str, path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def _registry_path(path: str | Path | None = None) -> Path:
    return Path(path or DEFAULT_HOLDOUT_EXTENSION_REGISTRY_PATH).resolve(
        strict=False
    )


def load_holdout_extension_registry(
    path: str | Path | None = None,
) -> dict[str, Any]:
    target = _registry_path(path)
    if not target.is_file():
        return {
            "artifact_type": "kinace_v3_holdout_extension_registry",
            "schema_version": SCHEMA_VERSION,
            "records": [],
            "updated_at_utc": None,
        }
    payload = _mapping(_load_json(target, "holdout extension registry"), "registry")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ResearchHoldoutError("holdout extension registry records must be a list")
    return {
        "artifact_type": "kinace_v3_holdout_extension_registry",
        "schema_version": int(payload.get("schema_version") or SCHEMA_VERSION),
        "records": [dict(_mapping(item, "registry record")) for item in records],
        "updated_at_utc": payload.get("updated_at_utc"),
    }


def _write_holdout_extension_registry(
    payload: Mapping[str, Any],
    path: str | Path | None = None,
) -> Path:
    return _write_json(payload, _registry_path(path))


def _record_database(value: str | Path) -> str:
    return str(validate_database_path(value).resolve(strict=False)).casefold()


def register_holdout_extension_freeze(
    freeze: Mapping[str, Any],
    *,
    database: str | Path,
    freeze_path: str | Path,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    registry = load_holdout_extension_registry(registry_path)
    database_key = _record_database(database)
    freeze_id = str(freeze.get("freeze_id") or "")
    if not freeze_id:
        raise ResearchHoldoutError("freeze_id is required for registry registration")
    records = registry["records"]
    existing = next(
        (
            record
            for record in records
            if str(record.get("database_key") or "") == database_key
            and str(record.get("freeze_id") or "") == freeze_id
        ),
        None,
    )
    now = _utc_now()
    record = {
        "database": str(validate_database_path(database).resolve(strict=False)),
        "database_key": database_key,
        "candidate_signature": str(freeze.get("candidate_signature") or ""),
        "evaluation_protocol_hash": str(
            freeze.get("evaluation_protocol_hash") or ""
        ),
        "freeze_id": freeze_id,
        "freeze_path": str(Path(freeze_path).resolve(strict=False)),
        "policy_id": str(freeze.get("policy_id") or ""),
        "status": "pending",
        "registered_at_utc": (
            str(existing.get("registered_at_utc") or now) if existing else now
        ),
        "last_checked_at_utc": (
            existing.get("last_checked_at_utc") if existing else None
        ),
        "last_extension_id": existing.get("last_extension_id") if existing else None,
        "last_result_path": existing.get("last_result_path") if existing else None,
        "next_check_utc": None,
        "updated_at_utc": now,
    }
    if existing is None:
        records.append(record)
    else:
        existing.update(record)
    registry["updated_at_utc"] = now
    _write_holdout_extension_registry(registry, registry_path)
    return record


def update_holdout_extension_registry(
    *,
    database: str | Path,
    freeze_id: str,
    status: str,
    extension_id: str | None = None,
    result_path: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    registry = load_holdout_extension_registry(registry_path)
    database_key = _record_database(database)
    record = next(
        (
            item
            for item in registry["records"]
            if str(item.get("database_key") or "") == database_key
            and str(item.get("freeze_id") or "") == str(freeze_id)
        ),
        None,
    )
    if record is None:
        return {"updated": False, "reason": "freeze is not registered"}
    now = _utc_now()
    record["status"] = str(status)
    record["last_checked_at_utc"] = now
    record["last_extension_id"] = extension_id
    if result_path is not None:
        record["last_result_path"] = str(Path(result_path).resolve(strict=False))
    record["next_check_utc"] = now if status == "awaiting_data" else None
    record["updated_at_utc"] = now
    registry["updated_at_utc"] = now
    _write_holdout_extension_registry(registry, registry_path)
    return {"updated": True, **record}


def pending_holdout_extension_record(
    database: str | Path,
    *,
    registry_path: str | Path | None = None,
) -> dict[str, Any] | None:
    records = pending_holdout_extension_records(
        database=database,
        registry_path=registry_path,
    )
    return records[0] if records else None


def pending_holdout_extension_records(
    *,
    database: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    registry = load_holdout_extension_registry(registry_path)
    database_key = _record_database(database) if database is not None else None
    candidates = [
        record
        for record in registry["records"]
        if (
            database_key is None
            or str(record.get("database_key") or "") == database_key
        )
        and str(record.get("status") or "") in {"pending", "awaiting_data"}
        and Path(str(record.get("freeze_path") or "")).is_file()
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda item: str(item.get("updated_at_utc") or ""), reverse=True)
    return candidates


def normalize_holdout_extension_policy(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(config or {})
    unknown = sorted(set(source) - set(DEFAULT_HOLDOUT_EXTENSION_POLICY))
    if unknown:
        raise ResearchHoldoutError(
            "unknown holdout extension policy keys: " + ", ".join(unknown)
        )
    result = deepcopy(DEFAULT_HOLDOUT_EXTENSION_POLICY)
    result.update(source)
    for key in (
        "initial_holdout_sessions",
        "min_trade_count",
        "extension_step_sessions",
        "max_total_sessions",
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ResearchHoldoutError(f"{key} must be a positive integer")
        result[key] = int(value)
    for key in (
        "freeze_candidate_before_extension",
        "stop_on_trade_count_only",
        "stop_when_trade_count_reached",
        "reject_if_not_reached_by_max_sessions",
        "retroactive",
    ):
        if not isinstance(result[key], bool):
            raise ResearchHoldoutError(f"{key} must be boolean")
    if result["version"] != HOLDOUT_EXTENSION_POLICY_VERSION:
        raise ResearchHoldoutError(
            "unsupported holdout extension policy version: "
            + str(result["version"])
        )
    if result["scope"] != "fresh_fixed_holdout":
        raise ResearchHoldoutError("holdout extension scope must be fresh_fixed_holdout")
    if (
        result["trigger"]
        != "all_non_trade_holdout_gates_passed_and_trade_count_lt_30"
    ):
        raise ResearchHoldoutError("unsupported holdout extension trigger")
    if not result["freeze_candidate_before_extension"]:
        raise ResearchHoldoutError("candidate freeze must be required")
    if not result["stop_on_trade_count_only"]:
        raise ResearchHoldoutError("stop decision must use trade count only")
    if not result["stop_when_trade_count_reached"]:
        raise ResearchHoldoutError("extension must stop when trade count is reached")
    if not result["reject_if_not_reached_by_max_sessions"]:
        raise ResearchHoldoutError("maximum-session rejection must be required")
    if result["retroactive"]:
        raise ResearchHoldoutError("holdout extension policy must not be retroactive")
    if result["initial_holdout_sessions"] >= result["max_total_sessions"]:
        raise ResearchHoldoutError(
            "max_total_sessions must exceed initial_holdout_sessions"
        )
    if result["extension_step_sessions"] != 1:
        raise ResearchHoldoutError("extension_step_sessions must be 1")
    return result


def holdout_extension_policy_identity(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return normalize_holdout_extension_policy(config)


def holdout_extension_policy_id(config: Mapping[str, Any] | None = None) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            holdout_extension_policy_identity(config),
            trailing_newline=False,
        )
    )


def objective_without_trade_count(
    objective: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an objective that ignores the trade-count minimum."""
    payload = deepcopy(dict(objective))
    constraints = payload.get("constraints")
    if isinstance(constraints, Mapping) and isinstance(
        constraints.get("min_trade_count"), Mapping
    ):
        updated_constraints = deepcopy(dict(constraints))
        updated_constraint = deepcopy(
            dict(_mapping(updated_constraints["min_trade_count"], "min_trade_count"))
        )
        updated_constraint["threshold"] = 0
        updated_constraints["min_trade_count"] = updated_constraint
        payload["constraints"] = updated_constraints
        return payload
    hard_gates = payload.get("hard_gates")
    if isinstance(hard_gates, Mapping):
        updated_gates = deepcopy(dict(hard_gates))
        updated_gates["min_trade_count"] = 0
        payload["hard_gates"] = updated_gates
        return payload
    raise ResearchHoldoutError(
        "objective must define constraints.min_trade_count or hard_gates.min_trade_count"
    )


def build_holdout_extension_freeze(
    *,
    loop_id: str,
    candidate_signature: str,
    protocol_candidate_id: str,
    evaluation_protocol_hash: str,
    model_config: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
    objective: Mapping[str, Any],
    initial_holdout: Mapping[str, Any],
    initial_metrics: Mapping[str, Any],
    initial_objective: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    holdout_access_id: str | None = None,
    track: str = FORMAL_TRACK,
    holdout_access_registry_path: str | Path | None = None,
) -> dict[str, Any]:
    normalized_policy = normalize_holdout_extension_policy(policy)
    access_id = str(holdout_access_id or "").strip()
    if not access_id:
        raise ResearchHoldoutError(
            "holdout_access_id is required to freeze a holdout extension"
        )
    normalized_track = str(track).strip().casefold()
    if normalized_track not in {"formal", "sandbox"}:
        raise ResearchHoldoutError("holdout extension track must be formal or sandbox")
    initial = {
        "start": str(initial_holdout.get("start") or ""),
        "end": str(initial_holdout.get("end") or ""),
        "sessions": int(initial_holdout.get("sessions") or 0),
    }
    if not initial["start"] or not initial["end"]:
        raise ResearchHoldoutError("initial holdout start and end are required")
    if initial["sessions"] != normalized_policy["initial_holdout_sessions"]:
        raise ResearchHoldoutError(
            "initial holdout session count does not match the frozen policy"
        )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "loop_id": str(loop_id),
        "candidate_signature": str(candidate_signature),
        "protocol_candidate_id": str(protocol_candidate_id),
        "evaluation_protocol_hash": str(evaluation_protocol_hash),
        "model_config": deepcopy(dict(model_config)),
        "backtest_config": deepcopy(dict(backtest_config)),
        "objective": deepcopy(dict(objective)),
        "initial_holdout": initial,
        "initial_metrics": deepcopy(dict(initial_metrics)),
        "holdout_extension_policy": normalized_policy,
        "policy_id": holdout_extension_policy_id(normalized_policy),
        "holdout_access_id": access_id,
        "track": normalized_track,
        "holdout_access_registry_path": str(
            resolve_holdout_access_registry_path(
                holdout_access_registry_path,
                track=normalized_track,
            )
        ),
    }
    return {
        "artifact_type": "kinace_v3_holdout_extension_freeze",
        **identity,
        "freeze_id": sha256_bytes(
            canonical_json_bytes(identity, trailing_newline=False)
        ),
        "initial_objective": deepcopy(dict(initial_objective)),
        "created_at_utc": _utc_now(),
        "production_ready": False,
        "production_changed": False,
    }


def write_holdout_extension_freeze(
    freeze: Mapping[str, Any],
    path: str | Path,
) -> dict[str, Any]:
    target = _write_json(freeze, path)
    return {
        "freeze_path": str(target),
        "freeze_sha256": sha256_file(target),
    }


def _freeze_identity(freeze: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "loop_id": str(freeze.get("loop_id") or ""),
        "candidate_signature": str(freeze.get("candidate_signature") or ""),
        "protocol_candidate_id": str(freeze.get("protocol_candidate_id") or ""),
        "evaluation_protocol_hash": str(freeze.get("evaluation_protocol_hash") or ""),
        "model_config": deepcopy(
            dict(_mapping(freeze.get("model_config"), "freeze.model_config"))
        ),
        "backtest_config": deepcopy(
            dict(_mapping(freeze.get("backtest_config"), "freeze.backtest_config"))
        ),
        "objective": deepcopy(
            dict(_mapping(freeze.get("objective"), "freeze.objective"))
        ),
        "initial_holdout": deepcopy(
            dict(_mapping(freeze.get("initial_holdout"), "freeze.initial_holdout"))
        ),
        "initial_metrics": deepcopy(
            dict(_mapping(freeze.get("initial_metrics"), "freeze.initial_metrics"))
        ),
        "holdout_extension_policy": normalize_holdout_extension_policy(
            _mapping(
                freeze.get("holdout_extension_policy"),
                "freeze.holdout_extension_policy",
            )
        ),
        "policy_id": str(freeze.get("policy_id") or ""),
        "holdout_access_id": str(freeze.get("holdout_access_id") or ""),
        "track": str(freeze.get("track") or ""),
        "holdout_access_registry_path": str(
            freeze.get("holdout_access_registry_path") or ""
        ),
    }


def _first_trade_count_date(
    trades: Sequence[Mapping[str, Any]],
    candidate_dates: Sequence[str],
    min_trade_count: int,
) -> str | None:
    completed = sorted(
        str(trade.get("exit_date") or "")
        for trade in trades
        if isinstance(trade, Mapping) and trade.get("exit_date")
    )
    if len(completed) < min_trade_count:
        return None
    target_exit_date = completed[min_trade_count - 1]
    return next(
        (date for date in candidate_dates if date >= target_exit_date),
        None,
    )


def _markdown_report(payload: Mapping[str, Any]) -> str:
    metrics = payload.get("metrics") or {}
    objective = payload.get("objective") or {}
    extension = payload.get("extension") or {}
    lines = [
        "# Kinace Holdout Extension Evaluation",
        "",
        f"- Status: `{payload['status']}`",
        f"- Freeze ID: `{payload['freeze_id']}`",
        f"- Candidate: `{payload['candidate_signature']}`",
        f"- Protocol hash: `{payload['evaluation_protocol_hash']}`",
        "- Initial Holdout: "
        f"`{extension.get('initial_start')}` to `{extension.get('initial_end')}`",
        f"- Extension end: `{extension.get('end')}`",
        f"- Total sessions: `{extension.get('total_sessions')}`",
        f"- Total trades: `{metrics.get('trade_count')}`",
        f"- Unique decision dates: `{payload.get('unique_decision_dates')}`",
        f"- End reason: `{extension.get('end_reason')}`",
        f"- Objective status: `{objective.get('status')}`",
        f"- Hard gate failures: `{', '.join(objective.get('hard_gate_failures') or [])}`",
        f"- Production ready: `{payload['production_ready']}`",
        f"- Production changed: `{payload['production_changed']}`",
        "",
    ]
    return "\n".join(lines)


def _update_candidate_status(
    database: str | Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    target = validate_database_path(database)
    with sqlite3.connect(str(target)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "research_candidates" not in tables:
            return {"updated": False, "reason": "research_candidates table is absent"}
        row = connection.execute(
            """
            SELECT candidate_signature, protocol_candidate_id,
                   evaluation_protocol_hash
            FROM research_candidates
            WHERE candidate_signature=?
            """,
            (payload["candidate_signature"],),
        ).fetchone()
        if row is None:
            return {"updated": False, "reason": "candidate is not registered"}
        if str(row["evaluation_protocol_hash"] or "") != str(
            payload["evaluation_protocol_hash"]
        ):
            return {"updated": False, "reason": "candidate protocol hash does not match"}
        status = str(payload["status"])
        if status == "passed":
            stage = "awaiting_candidate_audit"
            holdout_status = "challenger_ready"
        elif status == "awaiting_data":
            stage = "holdout_extension_pending"
            holdout_status = "holdout_extension_pending"
        else:
            stage = "holdout_extension_rejected"
            holdout_status = "holdout_extension_rejected"
        connection.execute(
            """
            UPDATE research_candidates
            SET stage=?, holdout_status=?, holdout_metrics_json=?,
                updated_at_utc=?
            WHERE candidate_signature=?
            """,
            (
                stage,
                holdout_status,
                canonical_json_bytes(
                    payload.get("metrics") or {},
                    trailing_newline=False,
                ).decode("utf-8"),
                _utc_now(),
                payload["candidate_signature"],
            ),
        )
        connection.commit()
    return {"updated": True, "stage": stage, "holdout_status": holdout_status}


def run_holdout_extension(
    database: str | Path | None,
    freeze_path: str | Path,
    *,
    snapshot_dir: str | Path,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_EXTENSION_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_EXTENSION_REPORT_DIR,
    registry_path: str | Path | None = None,
    holdout_access_registry_path: str | Path | None = None,
    code_version: str = "kinace-v3",
    portfolio_runner: Callable[..., Mapping[str, Any]] = run_portfolio_backtest,
) -> dict[str, Any]:
    freeze = _mapping(_load_json(freeze_path, "holdout extension freeze"), "freeze")
    expected_freeze_id = sha256_bytes(
        canonical_json_bytes(_freeze_identity(freeze), trailing_newline=False)
    )
    if str(freeze.get("freeze_id") or "") != expected_freeze_id:
        raise ResearchHoldoutError("holdout extension freeze hash does not match")
    policy = normalize_holdout_extension_policy(freeze.get("holdout_extension_policy"))
    if str(freeze.get("policy_id") or "") != holdout_extension_policy_id(policy):
        raise ResearchHoldoutError("frozen holdout extension policy hash does not match")
    access_id = str(freeze.get("holdout_access_id") or "").strip()
    track = str(freeze.get("track") or "").strip().casefold()
    if not access_id:
        raise ResearchHoldoutError("freeze is missing holdout_access_id")
    if track not in {"formal", "sandbox"}:
        raise ResearchHoldoutError("freeze has an invalid holdout track")
    frozen_access_registry_path = str(
        freeze.get("holdout_access_registry_path") or ""
    ).strip()
    access_registry_path = (
        holdout_access_registry_path or frozen_access_registry_path or None
    )
    if (
        holdout_access_registry_path is not None
        and frozen_access_registry_path
        and Path(holdout_access_registry_path).resolve(strict=False)
        != Path(frozen_access_registry_path).resolve(strict=False)
    ):
        raise ResearchHoldoutError(
            "holdout access registry override does not match the frozen authorization"
        )
    resolved_access_registry_path = resolve_holdout_access_registry_path(
        access_registry_path,
        track=track,
    )
    model_config = validate_candidate_config(
        _mapping(freeze.get("model_config"), "freeze.model_config")
    )
    backtest_config = normalize_backtest_config(
        _mapping(freeze.get("backtest_config"), "freeze.backtest_config")
    )
    initial = _mapping(freeze.get("initial_holdout"), "freeze.initial_holdout")
    initial_start = str(initial.get("start") or "")
    initial_end = str(initial.get("end") or "")
    if not initial_start or not initial_end:
        raise ResearchHoldoutError("freeze is missing the initial holdout window")
    initial_sessions = int(initial.get("sessions") or 0)
    target = database_path() if database is None else validate_database_path(database)
    snapshots = discover_snapshots(snapshot_dir, start_date=initial_start)
    dates = [str(item["decision_date"]) for item in snapshots]
    if len(dates) < initial_sessions:
        raise ResearchHoldoutError("snapshot set does not contain the frozen initial Holdout")
    if dates[initial_sessions - 1] != initial_end:
        raise ResearchHoldoutError("frozen initial Holdout end does not match snapshots")
    max_dates = dates[: policy["max_total_sessions"]]
    extension_key = sha256_bytes(
        canonical_json_bytes(
            {
                "freeze_id": freeze.get("freeze_id"),
                "max_end": max_dates[-1] if max_dates else initial_end,
                "available_sessions": len(max_dates),
            },
            trailing_newline=False,
        )
    )
    run_dir = Path(output_dir).resolve(strict=False) / extension_key
    run_dir.mkdir(parents=True, exist_ok=True)
    max_end = max_dates[-1] if max_dates else initial_end
    bars = load_market_bars(target)
    access_binding = bind_holdout_extension_access(
        holdout_access_id=access_id,
        candidate=freeze.get("candidate_signature"),
        protocol=freeze.get("evaluation_protocol_hash"),
        extension_window={
            "start": initial_start,
            "end": max_end,
            "sessions": len(max_dates),
        },
        freeze_id=str(freeze.get("freeze_id") or ""),
        extension_policy_id=str(freeze.get("policy_id") or ""),
        registry_path=resolved_access_registry_path,
        track=track,
    )

    max_report = portfolio_runner(
        target,
        model_config,
        snapshot_dir,
        start_date=initial_start,
        end_date=max_end,
        bars=bars,
        config=backtest_config,
        output_path=run_dir / "max-window.json",
        code_version=code_version,
    )
    max_trades = [
        trade
        for trade in max_report.get("trades") or []
        if isinstance(trade, Mapping)
    ]
    stop_date = _first_trade_count_date(
        max_trades,
        max_dates,
        policy["min_trade_count"],
    )
    final_report = max_report
    end_reason = "max_sessions_reached"
    if stop_date is not None and stop_date != max_end:
        final_report = portfolio_runner(
            target,
            model_config,
            snapshot_dir,
            start_date=initial_start,
            end_date=stop_date,
            bars=bars,
            config=backtest_config,
            output_path=run_dir / f"final-{stop_date}.json",
            code_version=code_version,
        )
        end_reason = "trade_count_reached"
    elif stop_date is not None:
        end_reason = "trade_count_reached"

    frozen_objective = _mapping(freeze.get("objective"), "freeze.objective")
    objective = score_portfolio_objective(final_report["metrics"], frozen_objective)
    if stop_date is not None:
        status = str(objective["status"])
    elif len(max_dates) < policy["max_total_sessions"]:
        status = "awaiting_data"
        end_reason = "additional_sessions_required"
    elif policy["reject_if_not_reached_by_max_sessions"]:
        status = "rejected"
    else:
        status = "review_required"

    extension_end = str((final_report.get("date_range") or {}).get("end") or max_end)
    total_sessions = sum(date <= extension_end for date in max_dates)
    trades = [
        trade
        for trade in final_report.get("trades") or []
        if isinstance(trade, Mapping)
    ]
    unique_decision_dates = len(
        {
            str(trade.get("decision_date") or "")
            for trade in trades
            if trade.get("decision_date")
        }
    )
    identity = {
        "freeze_id": freeze.get("freeze_id"),
        "candidate_signature": freeze.get("candidate_signature"),
        "evaluation_protocol_hash": freeze.get("evaluation_protocol_hash"),
        "extension_end": extension_end,
        "status": status,
    }
    extension_id = sha256_bytes(
        canonical_json_bytes(identity, trailing_newline=False)
    )
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(report_dir).resolve(strict=False)
        / f"holdout-extension-{extension_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else artifact_path.with_suffix(".md")
    )
    payload = {
        "artifact_type": "kinace_v3_holdout_extension_result",
        "schema_version": SCHEMA_VERSION,
        "extension_id": extension_id,
        "status": status,
        "freeze_id": freeze.get("freeze_id"),
        "candidate_signature": freeze.get("candidate_signature"),
        "evaluation_protocol_hash": freeze.get("evaluation_protocol_hash"),
        "policy_id": freeze.get("policy_id"),
        "holdout_access_id": access_id,
        "holdout_access_read_count": access_binding["read_count"],
        "holdout_track": track,
        "holdout_access_registry_path": str(resolved_access_registry_path),
        "holdout_access_binding": access_binding,
        "database": str(target),
        "snapshot_dir": str(Path(snapshot_dir).resolve(strict=False)),
        "extension": {
            "initial_start": initial_start,
            "initial_end": initial_end,
            "end": extension_end,
            "initial_sessions": initial_sessions,
            "total_sessions": total_sessions,
            "maximum_sessions": policy["max_total_sessions"],
            "end_reason": end_reason,
        },
        "metrics": dict(final_report.get("metrics") or {}),
        "objective": objective,
        "unique_decision_dates": unique_decision_dates,
        "max_window_output_path": (
            str(max_report.get("output_path"))
            if max_report.get("output_path") is not None
            else None
        ),
        "final_window_output_path": (
            str(final_report.get("output_path"))
            if final_report.get("output_path") is not None
            else None
        ),
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    payload["database_update"] = _update_candidate_status(target, payload)
    payload["registry_update"] = update_holdout_extension_registry(
        database=target,
        freeze_id=str(freeze.get("freeze_id") or ""),
        status=status,
        extension_id=extension_id,
        result_path=written,
        registry_path=registry_path,
    )
    return payload


__all__ = [
    "DEFAULT_EXTENSION_OUTPUT_DIR",
    "DEFAULT_EXTENSION_REPORT_DIR",
    "DEFAULT_HOLDOUT_EXTENSION_REGISTRY_PATH",
    "DEFAULT_HOLDOUT_EXTENSION_POLICY",
    "HOLDOUT_EXTENSION_POLICY_VERSION",
    "ResearchHoldoutError",
    "build_holdout_extension_freeze",
    "holdout_extension_policy_id",
    "holdout_extension_policy_identity",
    "load_holdout_extension_registry",
    "normalize_holdout_extension_policy",
    "objective_without_trade_count",
    "pending_holdout_extension_record",
    "pending_holdout_extension_records",
    "register_holdout_extension_freeze",
    "run_holdout_extension",
    "write_holdout_extension_freeze",
    "update_holdout_extension_registry",
]
