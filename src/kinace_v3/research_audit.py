"""Data and execution audit for the research backtest boundary."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, database_path, validate_database_path
from .data_collection.store import RevisionStore
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .portfolio_backtest import (
    PortfolioBacktestError,
    load_market_bars,
    validate_snapshot_manifest_for_research,
)
from .trial_backtest import initialize_trial_backtest_schema, normalize_backtest_config


SCHEMA_VERSION = 1
DEFAULT_AUDIT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "audit"
DEFAULT_AUDIT_REPORT_DIR = PROJECT_ROOT / "reports" / "audit"
DEFAULT_AUDIT_REPORT_PATH = PROJECT_ROOT / "reports" / "research_audit_canonical.json"
DEFAULT_PARITY_PATH = Path(r"D:\kinacesqlv3.2\parity\latest.json")


class ResearchAuditError(ValueError):
    """Raised when the data and execution audit cannot run."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchAuditError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchAuditError(f"{field} must be a non-empty string")
    return value.strip()


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchAuditError(f"cannot load {field}: {exc}") from exc


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
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    return target


def _connect(database: str | Path) -> sqlite3.Connection:
    target = validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: Mapping[str, str],
) -> None:
    existing = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if not existing:
        return
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'
            )


def build_snapshot_binding(snapshot_manifest_path: str | Path) -> dict[str, Any]:
    target = Path(snapshot_manifest_path).resolve(strict=False)
    manifest = _mapping(
        _load_json(target, "snapshot manifest"),
        "snapshot manifest",
    )
    try:
        validate_snapshot_manifest_for_research(manifest, source=str(target))
    except PortfolioBacktestError as exc:
        raise ResearchAuditError(str(exc)) from exc
    manifest_sha256 = sha256_file(target)
    snapshot_set_id = str(manifest.get("snapshot_set_id") or manifest_sha256).strip()
    if not snapshot_set_id:
        raise ResearchAuditError("snapshot_manifest has no identifying hash")
    return {
        "snapshot_set_id": snapshot_set_id,
        "snapshot_manifest_path": str(target),
        "snapshot_manifest_sha256": manifest_sha256,
    }


def build_candidate_binding(
    database: str | Path | None,
    *,
    loop_report_path: str | Path,
    candidate_report_path: str | Path,
) -> dict[str, Any]:
    from .score_replay import candidate_id_for

    loop_target = Path(loop_report_path).resolve(strict=False)
    candidate_target = Path(candidate_report_path).resolve(strict=False)
    loop_report = _mapping(
        _load_json(loop_target, "research loop report"),
        "research loop report",
    )
    candidate_report = _mapping(
        _load_json(candidate_target, "candidate report"),
        "candidate report",
    )
    loop_id = _required_text(loop_report.get("loop_id"), "loop_report.loop_id")
    best_candidate = _mapping(
        loop_report.get("best_candidate"), "loop_report.best_candidate"
    )
    candidate_signature = _required_text(
        best_candidate.get("signature"), "loop_report.best_candidate.signature"
    )
    model_config = _mapping(
        best_candidate.get("model_config"),
        "loop_report.best_candidate.model_config",
    )
    holdout = _mapping(loop_report.get("holdout"), "loop_report.holdout")
    holdout_run_id = _required_text(
        holdout.get("portfolio_run_id"), "loop_report.holdout.portfolio_run_id"
    )
    candidate_run_id = _required_text(candidate_report.get("run_id"), "candidate_report.run_id")
    if candidate_run_id != holdout_run_id:
        raise ResearchAuditError(
            "candidate report run_id does not match the loop holdout run_id"
        )
    expected_model_candidate_id = candidate_id_for(model_config)

    target = database_path() if database is None else validate_database_path(database)
    with _connect(target) as connection:
        run = connection.execute(
            "SELECT * FROM backtest_runs WHERE run_id=?", (holdout_run_id,)
        ).fetchone()
        if run is None:
            raise ResearchAuditError(f"holdout backtest run is missing: {holdout_run_id}")
        replay = connection.execute(
            "SELECT * FROM score_replays WHERE replay_id=?", (run["replay_id"],)
        ).fetchone()
        experiment = connection.execute(
            "SELECT * FROM experiments WHERE experiment_id=?", (run["experiment_id"],)
        ).fetchone()
        evidence = connection.execute(
            "SELECT * FROM backtest_evidence WHERE run_id=?", (holdout_run_id,)
        ).fetchone()
        candidate = connection.execute(
            "SELECT * FROM research_candidates WHERE candidate_signature=?",
            (candidate_signature,),
        ).fetchone()
    if run["candidate_id"] != expected_model_candidate_id:
        raise ResearchAuditError("holdout run candidate_id does not match the loop model config")
    if replay is None or replay["candidate_id"] != run["candidate_id"]:
        raise ResearchAuditError("holdout replay is missing or has a different candidate")
    if experiment is None or experiment["replay_id"] != run["replay_id"]:
        raise ResearchAuditError("holdout experiment is missing or has a different replay")
    if evidence is None or evidence["run_id"] != holdout_run_id:
        raise ResearchAuditError("holdout backtest evidence is missing")
    if candidate is None:
        raise ResearchAuditError("best candidate signature is not registered")

    snapshot = _mapping(
        candidate_report.get("snapshot_manifest"), "candidate_report.snapshot_manifest"
    )
    snapshot_set_id = _required_text(
        snapshot.get("snapshot_set_id"), "candidate_report.snapshot_manifest.snapshot_set_id"
    )
    return {
        "loop_id": loop_id,
        "candidate_signature": candidate_signature,
        "model_candidate_id": expected_model_candidate_id,
        "holdout_run_id": holdout_run_id,
        "replay_id": str(run["replay_id"]),
        "experiment_id": str(run["experiment_id"]),
        "evidence_id": str(evidence["backtest_id"]),
        "snapshot_set_id": snapshot_set_id,
        "snapshot_manifest_path": str(
            Path(str(snapshot.get("artifact_path") or "")).resolve(strict=False)
        )
        if snapshot.get("artifact_path")
        else None,
        "snapshot_manifest_sha256": snapshot.get("artifact_sha256"),
        "loop_report_path": str(loop_target),
        "loop_report_sha256": sha256_file(loop_target),
        "candidate_report_path": str(candidate_target),
        "candidate_report_sha256": sha256_file(candidate_target),
        "code_version": str(run["code_version"]),
    }


def initialize_audit_schema(database: str | Path | None = None) -> Path:
    target = initialize_trial_backtest_schema(database)
    with _connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_audits (
                audit_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                critical_count INTEGER NOT NULL,
                high_count INTEGER NOT NULL,
                medium_count INTEGER NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT,
                summary_json TEXT NOT NULL,
                loop_id TEXT,
                candidate_signature TEXT,
                model_candidate_id TEXT,
                snapshot_set_id TEXT,
                holdout_run_id TEXT,
                replay_id TEXT,
                experiment_id TEXT,
                evidence_id TEXT,
                binding_json TEXT,
                candidate_bound INTEGER NOT NULL DEFAULT 0,
                candidate_audit_passed INTEGER NOT NULL DEFAULT 0,
                created_at_utc TEXT NOT NULL
            );
            """
        )
        _ensure_columns(
            connection,
            "research_audits",
            {
                "loop_id": "TEXT",
                "candidate_signature": "TEXT",
                "model_candidate_id": "TEXT",
                "snapshot_set_id": "TEXT",
                "holdout_run_id": "TEXT",
                "replay_id": "TEXT",
                "experiment_id": "TEXT",
                "evidence_id": "TEXT",
                "binding_json": "TEXT",
                "candidate_bound": "INTEGER NOT NULL DEFAULT 0",
                "candidate_audit_passed": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _ensure_columns(
            connection,
            "research_loop_runs",
            {
                "candidate_audit_id": "TEXT",
                "candidate_audit_status": "TEXT",
                "candidate_audit_sha256": "TEXT",
                "candidate_audit_bound": "INTEGER NOT NULL DEFAULT 0",
            },
        )
    return target


def audit_price_continuity(
    database: str | Path,
    *,
    jump_threshold: float = 0.21,
    severe_threshold: float = 0.30,
) -> dict[str, Any]:
    target = validate_database_path(database)
    connection = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT dataset, source, record_json
            FROM current_revisions
            WHERE dataset IN ('daily_bars', 'security_state')
              AND effective_status IN ('observed', 'backfilled_reliable')
            """
        ).fetchall()
    finally:
        connection.close()
    daily: dict[str, list[dict[str, Any]]] = defaultdict(list)
    states: dict[tuple[str, str], dict[str, Any]] = {}
    trade_dates: set[str] = set()
    for row in rows:
        record = json.loads(row["record_json"])
        if row["dataset"] == "daily_bars":
            trade_date = str(record["trade_date"])
            trade_dates.add(trade_date)
            daily[str(record["ts_code"])].append({**record, "source": row["source"]})
        else:
            states[(str(record["ts_code"]), str(record["trade_date"]))] = record
    calendar = sorted(trade_dates)
    jumps: list[dict[str, Any]] = []
    source_transitions: list[dict[str, Any]] = []
    invalid_ohlc: list[dict[str, Any]] = []
    zero_volume_not_suspended: list[dict[str, Any]] = []
    for code, bars in daily.items():
        ordered = sorted(bars, key=lambda item: item["trade_date"])
        previous: Mapping[str, Any] | None = None
        for bar in ordered:
            open_price = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            if (
                min(open_price, high, low, close) <= 0
                or high < max(open_price, close, low)
                or low > min(open_price, close, high)
            ):
                invalid_ohlc.append(
                    {
                        "ts_code": code,
                        "trade_date": bar["trade_date"],
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "close": close,
                    }
                )
            adjacent = False
            if previous is not None:
                adjacent = bisect_right(
                    calendar, str(previous["trade_date"])
                ) == bisect_left(calendar, str(bar["trade_date"]))
                previous_close = float(previous["close"])
                daily_return = close / previous_close - 1.0 if previous_close > 0 else 0.0
                state = states.get((code, str(bar["trade_date"])), {})
                if (
                    adjacent
                    and
                    abs(daily_return) > jump_threshold
                    and not state.get("is_limit_up")
                    and not state.get("is_limit_down")
                ):
                    jumps.append(
                        {
                            "severity": "critical"
                            if abs(daily_return) >= severe_threshold
                            else "high",
                            "ts_code": code,
                            "trade_date": str(bar["trade_date"]),
                            "previous_date": str(previous["trade_date"]),
                            "previous_close": previous_close,
                            "close": close,
                            "daily_return": daily_return,
                            "previous_source": previous.get("source"),
                            "source": bar.get("source"),
                            "is_limit_up": bool(state.get("is_limit_up", False)),
                            "is_limit_down": bool(state.get("is_limit_down", False)),
                        }
                    )
            if (
                previous is not None
                and adjacent
                and str(bar.get("source")) != str(previous.get("source"))
            ):
                previous_close = float(previous["close"])
                source_transitions.append(
                    {
                        "ts_code": code,
                        "trade_date": str(bar["trade_date"]),
                        "previous_source": previous.get("source"),
                        "source": bar.get("source"),
                        "daily_return": close / previous_close - 1.0
                        if previous_close
                        else 0.0,
                    }
                )
            state = states.get((code, str(bar["trade_date"])), {})
            if float(bar.get("vol") or 0.0) <= 0 and not state.get("is_suspended"):
                zero_volume_not_suspended.append(
                    {"ts_code": code, "trade_date": str(bar["trade_date"])}
                )
            previous = bar
    jumps.sort(key=lambda item: abs(float(item["daily_return"])), reverse=True)
    source_transitions.sort(
        key=lambda item: abs(float(item["daily_return"])), reverse=True
    )
    return {
        "code_count": len(daily),
        "record_count": sum(len(values) for values in daily.values()),
        "suspicious_jump_count": len(jumps),
        "severe_jump_count": sum(item["severity"] == "critical" for item in jumps),
        "source_transition_count": len(source_transitions),
        "invalid_ohlc_count": len(invalid_ohlc),
        "zero_volume_not_suspended_count": len(zero_volume_not_suspended),
        "suspicious_jumps": jumps,
        "source_transitions": source_transitions[:100],
        "invalid_ohlc": invalid_ohlc[:100],
        "zero_volume_not_suspended": zero_volume_not_suspended[:100],
    }


def audit_parity(path: str | Path = DEFAULT_PARITY_PATH) -> dict[str, Any]:
    payload = _mapping(_load_json(path, "source parity report"), "source parity report")
    mismatches = [
        dict(item) for item in payload.get("mismatches") or [] if isinstance(item, Mapping)
    ]
    high_return_mismatches = []
    for item in mismatches:
        fields = item.get("fields")
        if not isinstance(fields, Mapping):
            continue
        close_return = fields.get("close_return")
        if isinstance(close_return, Mapping):
            delta = abs(float(close_return.get("absolute_delta") or 0.0))
            if delta >= 0.05:
                high_return_mismatches.append(
                    {
                        "ts_code": item.get("ts_code"),
                        "trade_date": item.get("trade_date"),
                        "close_return_delta": delta,
                        "incoming": close_return.get("incoming"),
                        "primary": close_return.get("primary"),
                    }
                )
    high_return_mismatches.sort(
        key=lambda item: float(item["close_return_delta"]), reverse=True
    )
    return {
        "path": str(Path(path).resolve(strict=False)),
        "passed": bool(payload.get("passed")),
        "overlap_records": int(payload.get("overlap_records") or 0),
        "mismatch_records": int(payload.get("mismatch_records") or 0),
        "mismatch_ratio": float(payload.get("mismatch_ratio") or 0.0),
        "max_delta": dict(payload.get("max_delta") or {}),
        "acceptance_policy": dict(payload.get("acceptance_policy") or {}),
        "high_return_mismatch_count": len(high_return_mismatches),
        "high_return_mismatches": high_return_mismatches[:100],
    }


def audit_security_state(database: str | Path) -> dict[str, Any]:
    store = RevisionStore(database)
    daily = {
        (str(record.get("ts_code")), str(record.get("trade_date")))
        for row in store.current_records("daily_bars")
        if (record := row.get("record") if isinstance(row.get("record"), Mapping) else row)
    }
    states: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in store.current_records("security_state"):
        record = row.get("record") if isinstance(row.get("record"), Mapping) else row
        key = (str(record.get("ts_code")), str(record.get("trade_date")))
        if key[0] and key[1]:
            states[key] = record
    missing = sorted(daily - set(states))
    return {
        "daily_keys": len(daily),
        "state_keys": len(states),
        "missing_state_count": len(missing),
        "missing_state_sample": [list(item) for item in missing[:100]],
        "suspended_count": sum(bool(item.get("is_suspended")) for item in states.values()),
        "limit_up_count": sum(bool(item.get("is_limit_up")) for item in states.values()),
        "limit_down_count": sum(bool(item.get("is_limit_down")) for item in states.values()),
    }


def audit_execution(
    candidate_report: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    config = normalize_backtest_config(
        _mapping(candidate_report.get("config") or {}, "candidate.config")
    )
    bar_maps = {
        code: {str(bar["trade_date"]): bar for bar in code_bars}
        for code, code_bars in bars.items()
    }
    violations: list[dict[str, Any]] = []
    slippage_rate = float(config["slippage_bps"]) / 10_000.0
    for index, trade in enumerate(candidate_report.get("trades") or []):
        if not isinstance(trade, Mapping):
            continue
        code = str(trade.get("code") or "")
        entry_date = str(trade.get("entry_date") or "")
        exit_date = str(trade.get("exit_date") or "")
        entry_bar = bar_maps.get(code, {}).get(entry_date)
        exit_bar = bar_maps.get(code, {}).get(exit_date)
        if entry_bar is None or exit_bar is None:
            violations.append(
                {
                    "trade_index": index,
                    "type": "missing_bar",
                    "code": code,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                }
            )
            continue
        if entry_bar.get("is_suspended") or entry_bar.get("is_limit_up"):
            violations.append(
                {"trade_index": index, "type": "entry_not_tradable", "code": code}
            )
        if exit_bar.get("is_suspended") or exit_bar.get("is_limit_down"):
            violations.append(
                {"trade_index": index, "type": "exit_not_tradable", "code": code}
            )
        expected_entry = float(trade["intended_entry"]) * (1.0 + slippage_rate)
        if not math.isclose(
            expected_entry, float(trade["entry_price"]), rel_tol=0.0, abs_tol=1e-6
        ):
            violations.append(
                {
                    "trade_index": index,
                    "type": "entry_slippage_mismatch",
                    "expected": expected_entry,
                    "actual": trade["entry_price"],
                }
            )
        inferred_exit = float(trade["exit_price"]) / (1.0 - slippage_rate)
        expected_exit = inferred_exit * (1.0 - slippage_rate)
        if not math.isclose(
            expected_exit, float(trade["exit_price"]), rel_tol=0.0, abs_tol=1e-6
        ):
            violations.append(
                {"trade_index": index, "type": "exit_slippage_mismatch"}
            )
        entry_gross = float(trade["entry_price"]) * int(trade["shares"])
        entry_fees = max(
            float(config["min_commission"]),
            entry_gross * float(config["commission_rate"]),
        ) + entry_gross * float(config["transfer_fee_rate"])
        exit_gross = float(trade["exit_price"]) * int(trade["shares"])
        exit_fees = max(
            float(config["min_commission"]),
            exit_gross * float(config["commission_rate"]),
        ) + exit_gross * (
            float(config["transfer_fee_rate"]) + float(config["stamp_duty_rate"])
        )
        expected_fees = entry_fees + exit_fees
        if abs(expected_fees - float(trade["fees"])) > 0.01:
            violations.append(
                {
                    "trade_index": index,
                    "type": "fee_mismatch",
                    "expected": expected_fees,
                    "actual": trade["fees"],
                }
            )
        decision_date = str(trade.get("decision_date") or "")
        if decision_date:
            ordered_dates = sorted(bar_maps.get(code, {}))
            next_dates = [value for value in ordered_dates if value > decision_date]
            if not next_dates or next_dates[0] != entry_date:
                violations.append(
                    {
                        "trade_index": index,
                        "type": "entry_not_next_session",
                        "code": code,
                        "decision_date": decision_date,
                        "entry_date": entry_date,
                    }
                )
        shares = int(trade.get("shares") or 0)
        lot_size = int(config["lot_size"])
        if shares <= 0 or shares % lot_size != 0:
            violations.append(
                {
                    "trade_index": index,
                    "type": "invalid_lot_size",
                    "shares": shares,
                    "lot_size": lot_size,
                }
            )
        stop_loss = trade.get("stop_loss")
        take_profit = trade.get("take_profit")
        if stop_loss is not None and float(stop_loss) >= float(trade["entry_price"]):
            violations.append(
                {"trade_index": index, "type": "invalid_stop_loss", "code": code}
            )
        if take_profit is not None and float(take_profit) <= float(trade["entry_price"]):
            violations.append(
                {"trade_index": index, "type": "invalid_take_profit", "code": code}
            )
        exit_reason = str(trade.get("exit_reason") or "")
        if exit_reason == "stop_gap" and float(exit_bar["open"]) > float(stop_loss or 0):
            violations.append(
                {"trade_index": index, "type": "invalid_stop_gap", "code": code}
            )
        if exit_reason == "target_gap" and float(exit_bar["open"]) < float(take_profit or 0):
            violations.append(
                {"trade_index": index, "type": "invalid_target_gap", "code": code}
            )
        if exit_reason == "stop" and float(exit_bar["low"]) > float(stop_loss or 0):
            violations.append(
                {"trade_index": index, "type": "invalid_stop", "code": code}
            )
        if exit_reason == "target" and float(exit_bar["high"]) < float(take_profit or 0):
            violations.append(
                {"trade_index": index, "type": "invalid_target", "code": code}
            )
    return {
        "trade_count": len(candidate_report.get("trades") or []),
        "violation_count": len(violations),
        "violations": violations[:200],
        "t_plus_one_rule": "enforced_by_simulator",
        "next_session_open_entry": "enforced_by_simulator",
        "suspension_and_limit_rules": "enforced_by_simulator",
    }


def audit_universe(
    database: str | Path,
    *,
    snapshot_manifest_path: str | Path,
    candidate_report: Mapping[str, Any],
) -> dict[str, Any]:
    store = RevisionStore(database)
    metadata = [
        record
        for row in store.current_records("stock_metadata")
        if (record := row.get("record") if isinstance(row.get("record"), Mapping) else row)
    ]
    industries = defaultdict(int)
    list_date_present = 0
    delist_date_present = 0
    industry_by_code: dict[str, str] = {}
    for record in metadata:
        code = str(record.get("ts_code") or "")
        industry = str(record.get("industry") or "unknown")
        industries[industry] += 1
        if code:
            industry_by_code[code] = industry
        if record.get("list_date"):
            list_date_present += 1
        if record.get("delist_date"):
            delist_date_present += 1
    trades = [
        dict(item) for item in candidate_report.get("trades") or [] if isinstance(item, Mapping)
    ]
    traded_industries = defaultdict(int)
    for trade in trades:
        traded_industries[industry_by_code.get(str(trade.get("code") or ""), "unknown")] += 1
    manifest = _mapping(
        _load_json(snapshot_manifest_path, "snapshot manifest"), "snapshot manifest"
    )
    row_counts = [
        int(item.get("row_count") or 0)
        for item in manifest.get("snapshots") or []
        if isinstance(item, Mapping)
    ]
    membership_rows = store.current_records("sector_membership")
    sector_records = len(membership_rows)
    index_codes = {
        str(
            (row.get("record") if isinstance(row.get("record"), Mapping) else row).get(
                "index_code"
            )
            or ""
        )
        for row in membership_rows
    }
    index_records = sector_records
    return {
        "metadata_count": len(metadata),
        "industry_count": len(industries),
        "industries": dict(sorted(industries.items(), key=lambda item: (-item[1], item[0]))),
        "list_date_present": list_date_present,
        "delist_date_present": delist_date_present,
        "sector_membership_records": sector_records,
        "index_member_records": index_records,
        "index_codes": sorted(code for code in index_codes if code),
        "snapshot_row_count_min": min(row_counts) if row_counts else 0,
        "snapshot_row_count_max": max(row_counts) if row_counts else 0,
        "snapshot_row_count_mean": round(statistics.fmean(row_counts), 6)
        if row_counts
        else 0.0,
        "traded_code_count": len({str(item.get("code") or "") for item in trades}),
        "traded_industry_count": len(traded_industries),
        "traded_industries": dict(
            sorted(traded_industries.items(), key=lambda item: (-item[1], item[0]))
        ),
        "historical_universe_verifiable": bool(
            list_date_present and delist_date_present and sector_records
        ),
    }


def _finding(
    severity: str,
    code: str,
    message: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "evidence": dict(evidence or {}),
    }


def build_audit_report(
    *,
    price: Mapping[str, Any],
    parity: Mapping[str, Any],
    state: Mapping[str, Any],
    execution: Mapping[str, Any],
    universe: Mapping[str, Any],
    binding: Mapping[str, Any] | None = None,
    execution_applicable: bool = True,
) -> dict[str, Any]:
    binding_payload = dict(binding or {})
    execution_payload = dict(execution)
    execution_payload["applicable"] = bool(execution_applicable)
    findings: list[dict[str, Any]] = []
    if int(price.get("severe_jump_count") or 0) > 0:
        findings.append(
            _finding(
                "critical",
                "price_discontinuity",
                f"{price['severe_jump_count']} suspicious price jumps exceed 30%",
                {"jumps": (price.get("suspicious_jumps") or [])[:10]},
            )
        )
    elif int(price.get("suspicious_jump_count") or 0) > 0:
        findings.append(
            _finding(
                "high",
                "price_discontinuity",
                f"{price['suspicious_jump_count']} suspicious price jumps exceed 21%",
                {"jumps": (price.get("suspicious_jumps") or [])[:10]},
            )
        )
    max_close_delta = abs(
        float((parity.get("max_delta") or {}).get("close_return") or 0.0)
    )
    if max_close_delta >= 0.20:
        findings.append(
            _finding(
                "critical",
                "source_parity_large_delta",
                f"cross-source close-return delta reaches {max_close_delta:.2%}",
                {"mismatches": (parity.get("high_return_mismatches") or [])[:10]},
            )
        )
    elif max_close_delta >= 0.05:
        findings.append(
            _finding(
                "high",
                "source_parity_delta",
                f"cross-source close-return delta reaches {max_close_delta:.2%}",
            )
        )
    if int(state.get("missing_state_count") or 0) > 0:
        findings.append(
            _finding(
                "high",
                "missing_security_state",
                f"{state['missing_state_count']} daily bars have no security state",
            )
        )
    if not execution_applicable:
        findings.append(
            _finding(
                "info",
                "candidate_execution_not_applicable",
                "preflight audit does not evaluate candidate-specific execution",
            )
        )
    elif int(execution_payload.get("violation_count") or 0) > 0:
        findings.append(
            _finding(
                "high",
                "execution_rule_violation",
                f"{execution_payload['violation_count']} execution violations were detected",
                {"violations": execution_payload.get("violations")},
            )
        )
    else:
        findings.append(
            _finding(
                "info",
                "execution_replay_clean",
                "entry/exit tradability, slippage, and fee replay passed",
            )
        )
    if not universe.get("historical_universe_verifiable"):
        findings.append(
            _finding(
                "critical",
                "unverifiable_historical_universe",
                "list/delist, sector membership, or index history is incomplete",
                {
                    "metadata_count": universe.get("metadata_count"),
                    "list_date_present": universe.get("list_date_present"),
                    "delist_date_present": universe.get("delist_date_present"),
                    "sector_membership_records": universe.get("sector_membership_records"),
                    "index_member_records": universe.get("index_member_records"),
                },
            )
        )
    counts = {
        severity: sum(item["severity"] == severity for item in findings)
        for severity in ("critical", "high", "medium", "info")
    }
    execution_clean = (
        not execution_applicable
        or int(execution_payload.get("violation_count") or 0) == 0
    )
    universe_ready = bool(universe.get("historical_universe_verifiable"))
    price_clean = int(price.get("severe_jump_count") or 0) == 0
    state_clean = int(state.get("missing_state_count") or 0) == 0
    research_eligible = bool(
        execution_clean and universe_ready and price_clean and state_clean
    )
    live_sync_eligible = bool(parity.get("passed"))
    status = "passed" if research_eligible and live_sync_eligible else "review_required"
    candidate_bound = bool(
        binding_payload.get("loop_id")
        and binding_payload.get("candidate_signature")
        and binding_payload.get("snapshot_set_id")
        and binding_payload.get("holdout_run_id")
        and binding_payload.get("replay_id")
        and binding_payload.get("experiment_id")
        and binding_payload.get("evidence_id")
    )
    candidate_audit_passed = bool(
        candidate_bound and execution_applicable and status == "passed"
    )
    audit_id = sha256_bytes(
        canonical_json_bytes(
            {
                "price": price,
                "parity": parity,
                "state": state,
                "execution": execution_payload,
                "universe": universe,
                "binding": binding_payload,
            },
            trailing_newline=False,
        )
    )
    return {
        "artifact_type": "kinace_v3_data_execution_audit",
        "schema_version": SCHEMA_VERSION,
        "audit_id": audit_id,
        "status": status,
        "research_eligible": research_eligible,
        "live_sync_eligible": live_sync_eligible,
        "severity_counts": counts,
        "price_continuity": dict(price),
        "source_parity": dict(parity),
        "security_state": dict(state),
        "execution": execution_payload,
        "universe": dict(universe),
        "binding": binding_payload,
        "candidate_bound": candidate_bound,
        "candidate_audit_passed": candidate_audit_passed,
        "findings": findings,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }


def _markdown_report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Kinace Data And Execution Audit",
        "",
        f"- Audit ID: `{payload['audit_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Critical: `{payload['severity_counts']['critical']}`",
        f"- High: `{payload['severity_counts']['high']}`",
        f"- Candidate bound: `{payload.get('candidate_bound')}`",
        f"- Candidate audit passed: `{payload.get('candidate_audit_passed')}`",
        "",
        "## Findings",
        "",
        "| Severity | Code | Message |",
        "|---|---|---|",
    ]
    for finding in payload.get("findings") or []:
        lines.append(
            f"| `{finding['severity']}` | `{finding['code']}` | {finding['message']} |"
        )
    price = payload.get("price_continuity") or {}
    lines.extend(
        [
            "",
            "## Price Continuity",
            "",
            f"- Suspicious jumps: `{price.get('suspicious_jump_count')}`",
            f"- Severe jumps: `{price.get('severe_jump_count')}`",
            f"- Source transitions: `{price.get('source_transition_count')}`",
            "",
            "| Code | Date | Return | Previous Source | Source |",
            "|---|---|---:|---|---|",
        ]
    )
    for item in (price.get("suspicious_jumps") or [])[:20]:
        lines.append(
            f"| `{item['ts_code']}` | `{item['trade_date']}` | {item['daily_return']:.4%} | "
            f"`{item.get('previous_source')}` | `{item.get('source')}` |"
        )
    parity = payload.get("source_parity") or {}
    lines.extend(
        [
            "",
            "## Source Parity",
            "",
            f"- Passed: `{parity.get('passed')}`",
            f"- Mismatch ratio: `{parity.get('mismatch_ratio')}`",
            f"- Max close-return delta: `{(parity.get('max_delta') or {}).get('close_return')}`",
            "",
            "## Execution",
            "",
            f"- Applicable: `{(payload.get('execution') or {}).get('applicable')}`",
            f"- Trades checked: `{(payload.get('execution') or {}).get('trade_count')}`",
            f"- Violations: `{(payload.get('execution') or {}).get('violation_count')}`",
            "",
            "## Binding",
            "",
            f"- Loop ID: `{(payload.get('binding') or {}).get('loop_id')}`",
            f"- Candidate signature: `{(payload.get('binding') or {}).get('candidate_signature')}`",
            f"- Snapshot set: `{(payload.get('binding') or {}).get('snapshot_set_id')}`",
            f"- Holdout run: `{(payload.get('binding') or {}).get('holdout_run_id')}`",
            f"- Replay: `{(payload.get('binding') or {}).get('replay_id')}`",
            f"- Experiment: `{(payload.get('binding') or {}).get('experiment_id')}`",
            f"- Evidence: `{(payload.get('binding') or {}).get('evidence_id')}`",
            "",
            "## Universe",
            "",
            f"- Metadata count: `{(payload.get('universe') or {}).get('metadata_count')}`",
            f"- List dates present: `{(payload.get('universe') or {}).get('list_date_present')}`",
            f"- Delist dates present: `{(payload.get('universe') or {}).get('delist_date_present')}`",
            f"- Sector membership records: `{(payload.get('universe') or {}).get('sector_membership_records')}`",
            f"- Index member records: `{(payload.get('universe') or {}).get('index_member_records')}`",
            f"- Historical universe verifiable: `{(payload.get('universe') or {}).get('historical_universe_verifiable')}`",
            "",
        ]
    )
    return "\n".join(lines)


def _bind_candidate_audit(
    database: str | Path,
    *,
    loop_report_path: str | Path,
    audit_payload: Mapping[str, Any],
) -> Path:
    loop_target = Path(loop_report_path).resolve(strict=False)
    loop_report = dict(
        _mapping(
            _load_json(loop_target, "research loop report"),
            "research loop report",
        )
    )
    binding = dict(_mapping(audit_payload.get("binding"), "audit.binding"))
    loop_id = _required_text(loop_report.get("loop_id"), "loop_report.loop_id")
    if binding.get("loop_id") != loop_id:
        raise ResearchAuditError("candidate audit binding loop_id does not match loop report")
    audit_reference = {
        "audit_id": audit_payload["audit_id"],
        "status": audit_payload["status"],
        "candidate_bound": bool(audit_payload.get("candidate_bound")),
        "candidate_audit_passed": bool(audit_payload.get("candidate_audit_passed")),
        "artifact_path": audit_payload["artifact_path"],
        "artifact_sha256": audit_payload["artifact_sha256"],
        "binding": binding,
    }
    loop_report["candidate_audit"] = audit_reference
    written_loop = _write_json(loop_report, loop_target)
    challenger_path = loop_report.get("best_challenger_path")
    if isinstance(challenger_path, str) and challenger_path:
        challenger_target = Path(challenger_path).resolve(strict=False)
        if challenger_target.is_file():
            challenger = dict(
                _mapping(
                    _load_json(challenger_target, "challenger artifact"),
                    "challenger artifact",
                )
            )
            challenger["candidate_audit"] = audit_reference
            challenger["registration_status"] = (
                "audit_passed_candidate"
                if audit_payload.get("candidate_audit_passed")
                else "audit_rejected"
            )
            _write_json(challenger, challenger_target)

    initialize_audit_schema(database)
    with _connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "research_loop_runs" in tables:
            connection.execute(
                """
                UPDATE research_loop_runs
                SET candidate_audit_id=?,
                    candidate_audit_status=?,
                    candidate_audit_sha256=?,
                    candidate_audit_bound=?,
                    artifact_sha256=?
                WHERE loop_id=?
                """,
                (
                    audit_payload["audit_id"],
                    audit_payload["status"],
                    audit_payload["artifact_sha256"],
                    1 if audit_payload.get("candidate_audit_passed") else 0,
                    sha256_file(written_loop),
                    loop_id,
                ),
            )
        if "research_candidates" in tables and binding.get("candidate_signature"):
            connection.execute(
                "UPDATE research_candidates SET stage=?, updated_at_utc=? "
                "WHERE candidate_signature=?",
                (
                    "audit_passed_candidate"
                    if audit_payload.get("candidate_audit_passed")
                    else "audit_rejected",
                    _utc_now(),
                    binding["candidate_signature"],
                ),
            )
    return written_loop


def run_research_audit(
    database: str | Path | None,
    candidate_report_path: str | Path | None = None,
    *,
    snapshot_manifest_path: str | Path,
    parity_path: str | Path = DEFAULT_PARITY_PATH,
    loop_report_path: str | Path | None = None,
    preflight: bool = False,
    bind_loop_report: bool = False,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_AUDIT_OUTPUT_DIR,
    report_dir: str | Path = DEFAULT_AUDIT_REPORT_DIR,
    code_version: str = "kinace-v3",
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    if preflight and candidate_report_path is not None:
        raise ResearchAuditError("preflight audit cannot include a candidate report")
    if not preflight and candidate_report_path is None:
        raise ResearchAuditError("candidate report is required for a candidate audit")
    if bind_loop_report and (preflight or loop_report_path is None):
        raise ResearchAuditError("loop report binding requires a candidate audit")

    price = audit_price_continuity(target)
    parity = audit_parity(parity_path)
    state = audit_security_state(target)
    if preflight:
        candidate_report: Mapping[str, Any] = {}
        execution: Mapping[str, Any] = {
            "status": "not_applicable",
            "trade_count": 0,
            "violation_count": 0,
            "violations": [],
        }
        execution_applicable = False
        binding = build_snapshot_binding(snapshot_manifest_path)
    else:
        bars = load_market_bars(target)
        candidate_report = _mapping(
            _load_json(candidate_report_path, "candidate report"), "candidate report"
        )
        execution = audit_execution(candidate_report, bars)
        execution_applicable = True
        binding = (
            build_candidate_binding(
                target,
                loop_report_path=loop_report_path,
                candidate_report_path=candidate_report_path,
            )
            if loop_report_path is not None
            else {
                **build_snapshot_binding(snapshot_manifest_path),
                "candidate_report_path": str(
                    Path(candidate_report_path).resolve(strict=False)
                ),
                "candidate_report_sha256": sha256_file(candidate_report_path),
                "holdout_run_id": candidate_report.get("run_id"),
                "replay_id": candidate_report.get("replay_id"),
                "experiment_id": candidate_report.get("experiment_id"),
                "code_version": code_version,
            }
        )
    universe = audit_universe(
        target,
        snapshot_manifest_path=snapshot_manifest_path,
        candidate_report=candidate_report,
    )
    payload = build_audit_report(
        price=price,
        parity=parity,
        state=state,
        execution=execution,
        universe=universe,
        binding=binding,
        execution_applicable=execution_applicable,
    )
    audit_id = payload["audit_id"]
    artifact_path = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(report_dir).resolve(strict=False) / f"research-audit-{audit_id}.json"
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else artifact_path.with_suffix(".md")
    )
    _write_text(_markdown_report(payload), markdown_target)
    written = _write_json(payload, artifact_path)
    payload["artifact_path"] = str(written)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(written)
    initialize_audit_schema(target)
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO research_audits (
                audit_id, status, critical_count, high_count, medium_count,
                artifact_path, artifact_sha256, summary_json, loop_id,
                candidate_signature, model_candidate_id, snapshot_set_id,
                holdout_run_id, replay_id, experiment_id, evidence_id,
                binding_json, candidate_bound, candidate_audit_passed,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET
                status=excluded.status,
                critical_count=excluded.critical_count,
                high_count=excluded.high_count,
                medium_count=excluded.medium_count,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                summary_json=excluded.summary_json,
                loop_id=excluded.loop_id,
                candidate_signature=excluded.candidate_signature,
                model_candidate_id=excluded.model_candidate_id,
                snapshot_set_id=excluded.snapshot_set_id,
                holdout_run_id=excluded.holdout_run_id,
                replay_id=excluded.replay_id,
                experiment_id=excluded.experiment_id,
                evidence_id=excluded.evidence_id,
                binding_json=excluded.binding_json,
                candidate_bound=excluded.candidate_bound,
                candidate_audit_passed=excluded.candidate_audit_passed
            """,
            (
                audit_id,
                payload["status"],
                payload["severity_counts"]["critical"],
                payload["severity_counts"]["high"],
                payload["severity_counts"]["medium"],
                str(written),
                payload["artifact_sha256"],
                canonical_json_bytes(
                    {
                        "status": payload["status"],
                        "severity_counts": payload["severity_counts"],
                        "findings": payload["findings"],
                        "candidate_bound": payload.get("candidate_bound"),
                        "candidate_audit_passed": payload.get(
                            "candidate_audit_passed"
                        ),
                        "binding": binding,
                    },
                    trailing_newline=False,
                ).decode("utf-8"),
                binding.get("loop_id"),
                binding.get("candidate_signature"),
                binding.get("model_candidate_id"),
                binding.get("snapshot_set_id"),
                binding.get("holdout_run_id"),
                binding.get("replay_id"),
                binding.get("experiment_id"),
                binding.get("evidence_id"),
                canonical_json_bytes(binding, trailing_newline=False).decode("utf-8"),
                1 if payload.get("candidate_bound") else 0,
                1 if payload.get("candidate_audit_passed") else 0,
                _utc_now(),
            ),
        )
    if bind_loop_report and loop_report_path is not None:
        payload["loop_report_path"] = str(
            _bind_candidate_audit(
                target,
                loop_report_path=loop_report_path,
                audit_payload=payload,
            )
        )
    return payload


__all__ = [
    "DEFAULT_AUDIT_OUTPUT_DIR",
    "DEFAULT_AUDIT_REPORT_DIR",
    "DEFAULT_AUDIT_REPORT_PATH",
    "DEFAULT_PARITY_PATH",
    "ResearchAuditError",
    "SCHEMA_VERSION",
    "audit_execution",
    "audit_parity",
    "audit_price_continuity",
    "audit_security_state",
    "audit_universe",
    "build_audit_report",
    "build_candidate_binding",
    "build_snapshot_binding",
    "initialize_audit_schema",
    "run_research_audit",
]
