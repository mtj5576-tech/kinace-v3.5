"""D1, D3, and D5 requalification checks for bound research evidence."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import database_path, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .research_loop import _candidate_signature
from .research_protocol import (
    protocol_candidate_id,
    recompute_evaluation_protocol_hash,
    required_protocol_field_names,
)
from .score_replay import candidate_id_for


SCHEMA_VERSION = 1


class ResearchQualificationError(ValueError):
    """Raised when a qualification artifact cannot be evaluated."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchQualificationError(f"{field} must be an object")
    return value


def _load_json(path: str | Path, field: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchQualificationError(f"cannot load {field}: {exc}") from exc


def _write_text(text: str, path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    return target


def _row(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Mapping[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _json_column(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _sha256_if_file(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    target = Path(path)
    return sha256_file(target) if target.is_file() else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _connect(database: str | Path) -> sqlite3.Connection:
    target = validate_database_path(database)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _d1_checks(
    connection: sqlite3.Connection,
    *,
    loop_report_path: str | Path,
    loop_report: Mapping[str, Any],
    candidate_audit: Mapping[str, Any],
) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    loop_id = str(loop_report.get("loop_id") or "")
    best = _mapping(loop_report.get("best_candidate"), "loop_report.best_candidate")
    holdout = _mapping(loop_report.get("holdout"), "loop_report.holdout")
    candidate_signature = str(best.get("signature") or "")
    holdout_run_id = str(holdout.get("portfolio_run_id") or "")
    protocol_value = loop_report.get("evaluation_protocol")
    protocol = protocol_value if isinstance(protocol_value, Mapping) else {}
    binding_value = loop_report.get("protocol_binding")
    protocol_binding = binding_value if isinstance(binding_value, Mapping) else {}
    stored_protocol_hash = str(loop_report.get("evaluation_protocol_hash") or "")
    recomputed_protocol_hash = ""
    if protocol:
        try:
            recomputed_protocol_hash = recompute_evaluation_protocol_hash(protocol)
        except (TypeError, ValueError):
            recomputed_protocol_hash = ""
    required_fields = protocol.get("required_evaluation_fields")
    required_fields = required_fields if isinstance(required_fields, Mapping) else {}
    candidate_protocol_id = str(best.get("protocol_candidate_id") or "")
    expected_candidate_protocol_id = protocol_candidate_id(
        stored_protocol_hash, candidate_signature
    )
    expected_model_id = candidate_id_for(
        _mapping(best.get("model_config"), "best_candidate.model_config")
    )
    candidate_report = _mapping(
        _load_json(
            str(holdout.get("portfolio_output_path") or ""),
            "holdout candidate report",
        ),
        "holdout candidate report",
    )
    candidate_snapshot = str(
        _mapping(
            candidate_report.get("snapshot_manifest") or {},
            "candidate_report.snapshot_manifest",
        ).get("snapshot_set_id")
        or ""
    )
    db_loop = _row(
        connection,
        "SELECT * FROM research_loop_runs WHERE loop_id=?",
        (loop_id,),
    )
    candidate_row = _row(
        connection,
        "SELECT * FROM research_candidates WHERE candidate_signature=?",
        (candidate_signature,),
    )
    holdout_run = _row(
        connection,
        "SELECT * FROM backtest_runs WHERE run_id=?",
        (holdout_run_id,),
    )
    replay = (
        _row(
            connection,
            "SELECT * FROM score_replays WHERE replay_id=?",
            (holdout_run["replay_id"],),
        )
        if holdout_run
        else None
    )
    experiment = (
        _row(
            connection,
            "SELECT * FROM experiments WHERE experiment_id=?",
            (holdout_run["experiment_id"],),
        )
        if holdout_run
        else None
    )
    evidence = _row(
        connection,
        "SELECT * FROM backtest_evidence WHERE run_id=?",
        (holdout_run_id,),
    )
    audit_ref = _mapping(loop_report.get("candidate_audit") or {}, "candidate_audit")
    audit_binding = _mapping(audit_ref.get("binding") or {}, "candidate_audit.binding")
    candidate_audit_row = _row(
        connection,
        "SELECT * FROM research_audits WHERE audit_id=?",
        (candidate_audit.get("audit_id"),),
    )
    fold_checks: list[dict[str, Any]] = []
    for fold in best.get("folds") or []:
        if not isinstance(fold, Mapping):
            continue
        run_id = str(fold.get("portfolio_run_id") or "")
        run = _row(connection, "SELECT * FROM backtest_runs WHERE run_id=?", (run_id,))
        trained_model = fold.get("trained_model_config")
        expected_fold_model = (
            candidate_id_for(trained_model) if isinstance(trained_model, Mapping) else None
        )
        fold_checks.append(
            {
                "fold": fold.get("fold"),
                "portfolio_run_id": run_id,
                "train_start": fold.get("train_start"),
                "train_end": fold.get("train_end"),
                "validation_start": fold.get("validation_start"),
                "validation_end": fold.get("validation_end"),
                "run_exists": run is not None,
                "trained_model_expected": expected_fold_model is not None,
                "candidate_matches": bool(
                    run
                    and expected_fold_model
                    and run.get("candidate_id") == expected_fold_model
                ),
            }
        )
    checks = {
        "loop_registered": db_loop is not None,
        "loop_artifact_hash_matches": bool(
            db_loop
            and sha256_file(loop_report_path)
            == str(db_loop.get("artifact_sha256") or "")
        ),
        "evaluation_protocol_present": bool(protocol),
        "evaluation_protocol_hash_matches": bool(
            stored_protocol_hash
            and recomputed_protocol_hash == stored_protocol_hash
            and protocol.get("evaluation_protocol_hash") == stored_protocol_hash
        ),
        "evaluation_protocol_loop_matches": str(protocol.get("loop_id") or "")
        == loop_id,
        "evaluation_protocol_required_fields_present": all(
            name in required_fields for name in required_protocol_field_names(protocol)
        ),
        "effective_protocol_artifact_hash_matches": bool(
            protocol_binding.get("effective_protocol_sha256")
            and _sha256_if_file(protocol_binding.get("effective_protocol_path"))
            == protocol_binding.get("effective_protocol_sha256")
        ),
        "candidate_manifest_hash_matches": bool(
            protocol_binding.get("candidate_manifest_sha256")
            and _sha256_if_file(protocol_binding.get("candidate_manifest_path"))
            == protocol_binding.get("candidate_manifest_sha256")
        ),
        "protocol_candidate_id_matches": bool(
            candidate_protocol_id
            and candidate_protocol_id == expected_candidate_protocol_id
        ),
        "candidate_registered_protocol_matches": bool(
            candidate_row
            and candidate_row.get("protocol_candidate_id") == candidate_protocol_id
            and candidate_row.get("evaluation_protocol_hash")
            == stored_protocol_hash
        ),
        "candidate_registered": candidate_row is not None,
        "candidate_base_signature_matches": bool(
            candidate_row
            and best.get("base_model_config")
            and _candidate_signature(
                _mapping(best.get("base_model_config"), "best.base_model_config"),
                _mapping(best.get("backtest_config"), "best.backtest_config"),
            )
            == candidate_signature
        ),
        "candidate_registered_model_matches_holdout": bool(
            candidate_row
            and candidate_id_for(_json_column(candidate_row["model_config_json"]))
            == expected_model_id
        ),
        "holdout_run_exists": holdout_run is not None,
        "holdout_candidate_matches": bool(
            holdout_run and holdout_run.get("candidate_id") == expected_model_id
        ),
        "replay_matches": bool(
            replay
            and holdout_run
            and replay.get("candidate_id") == holdout_run.get("candidate_id")
            and replay.get("replay_id") == holdout_run.get("replay_id")
        ),
        "experiment_matches": bool(
            experiment
            and holdout_run
            and experiment.get("candidate_id") == holdout_run.get("candidate_id")
            and experiment.get("replay_id") == holdout_run.get("replay_id")
            and experiment.get("experiment_id") == holdout_run.get("experiment_id")
        ),
        "evidence_matches": bool(
            evidence
            and holdout_run
            and evidence.get("candidate_id") == holdout_run.get("candidate_id")
            and evidence.get("replay_id") == holdout_run.get("replay_id")
        ),
        "fold_lineage_matches": bool(fold_checks)
        and all(
            item["run_exists"]
            and item["trained_model_expected"]
            and item["candidate_matches"]
            for item in fold_checks
        ),
        "candidate_audit_bound": bool(
            db_loop
            and db_loop.get("candidate_audit_bound") == 1
            and db_loop.get("candidate_audit_id") == candidate_audit.get("audit_id")
            and audit_ref.get("audit_id") == candidate_audit.get("audit_id")
            and audit_ref.get("candidate_audit_passed") is True
        ),
        "candidate_audit_evidence_matches": bool(
            candidate_audit_row
            and candidate_audit_row.get("candidate_signature") == candidate_signature
            and candidate_audit_row.get("holdout_run_id") == holdout_run_id
            and candidate_audit_row.get("snapshot_set_id") == candidate_snapshot
        ),
        "snapshot_binding_matches": bool(
            audit_binding.get("snapshot_set_id") == candidate_snapshot
            and audit_binding.get("candidate_signature") == candidate_signature
            and audit_binding.get("holdout_run_id") == holdout_run_id
        ),
    }
    return checks, fold_checks


def _d5_payload(
    connection: sqlite3.Connection,
    *,
    preflight: Mapping[str, Any],
    candidate_audit: Mapping[str, Any],
    loop_id: str,
) -> dict[str, Any]:
    db_loop = _row(connection, "SELECT * FROM research_loop_runs WHERE loop_id=?", (loop_id,))
    preflight_row = _row(
        connection,
        "SELECT * FROM research_audits WHERE audit_id=?",
        (preflight.get("audit_id"),),
    )
    candidate_audit_row = _row(
        connection,
        "SELECT * FROM research_audits WHERE audit_id=?",
        (candidate_audit.get("audit_id"),),
    )
    loop_report = _mapping(candidate_audit.get("binding") or {}, "candidate_audit.binding")
    checks = {
        "preflight_registered": preflight_row is not None,
        "preflight_passed": preflight.get("status") == "passed",
        "loop_registered": db_loop is not None,
        "candidate_audit_registered": candidate_audit_row is not None,
        "candidate_audit_passed": candidate_audit.get("status") == "passed"
        and candidate_audit.get("candidate_audit_passed") is True,
        "command_order": bool(
            preflight_row
            and db_loop
            and candidate_audit_row
            and preflight_row["created_at_utc"]
            <= db_loop["created_at_utc"]
            <= candidate_audit_row["created_at_utc"]
        ),
        "same_snapshot_set": bool(loop_report.get("snapshot_set_id")),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "command_order": [
            {
                "command": "research-audit --preflight",
                "audit_id": preflight.get("audit_id"),
                "created_at_utc": preflight_row["created_at_utc"]
                if preflight_row
                else None,
            },
            {
                "command": "research-loop",
                "loop_id": loop_id,
                "created_at_utc": db_loop["created_at_utc"] if db_loop else None,
            },
            {
                "command": "research-audit --candidate --bind-loop",
                "audit_id": candidate_audit.get("audit_id"),
                "created_at_utc": candidate_audit_row["created_at_utc"]
                if candidate_audit_row
                else None,
            },
        ],
    }


def _d3_payload(
    connection: sqlite3.Connection,
    *,
    release_gate: Mapping[str, Any],
    loop_report: Mapping[str, Any],
    candidate_audit: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = _mapping(
        release_gate.get("candidate_evidence") or {},
        "release_gate.candidate_evidence",
    )
    thresholds = _mapping(
        release_gate.get("thresholds") or {}, "release_gate.thresholds"
    )
    counts = _mapping(release_gate.get("counts") or {}, "release_gate.counts")
    best = _mapping(loop_report.get("best_candidate"), "loop_report.best_candidate")
    candidate_signature = str(best.get("signature") or "")
    holdout = _mapping(loop_report.get("holdout"), "loop_report.holdout")
    holdout_run_id = str(holdout.get("portfolio_run_id") or "")
    binding = _mapping(candidate_audit.get("binding") or {}, "candidate_audit.binding")
    loop_id = str(loop_report.get("loop_id") or "")
    bound_candidates = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT trade.candidate_id
            FROM backtest_trades AS trade
            JOIN backtest_runs AS run ON run.run_id = trade.run_id
            JOIN research_audits AS audit ON audit.holdout_run_id = run.run_id
            JOIN research_loop_runs AS loop ON loop.loop_id = audit.loop_id
            WHERE audit.status='passed'
              AND audit.candidate_audit_passed=1
              AND loop.holdout_status='challenger_ready'
              AND audit.loop_id=?
              AND audit.holdout_run_id=?
              AND trade.shares IS NOT NULL
              AND trade.shares<>0
              AND trade.candidate_id=run.candidate_id
            """,
            (loop_id, holdout_run_id),
        ).fetchall()
    ]
    checks = {
        "release_mode": release_gate.get("mode") == "release",
        "release_ready": release_gate.get("status") == "release_ready"
        and release_gate.get("ready") is True,
        "bound_trade_threshold": int(evidence.get("bound_trades") or 0)
        >= int(thresholds.get("min_nonzero_candidate_specific_trades") or 0),
        "independent_date_threshold": int(evidence.get("independent_trade_dates") or 0)
        >= int(thresholds.get("min_independent_trade_dates") or 0),
        "gate_uses_bound_evidence": int(
            counts.get("nonzero_candidate_specific_trades") or 0
        )
        == int(evidence.get("bound_trades") or 0),
        "candidate_audit_bound": candidate_audit.get("candidate_audit_passed") is True,
        "candidate_binding_matches": bool(
            binding.get("candidate_signature") == candidate_signature
            and binding.get("holdout_run_id") == holdout_run_id
        ),
        "bound_candidate_matches": bound_candidates == [str(binding.get("model_candidate_id"))]
        if bound_candidates
        else False,
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "evidence": dict(evidence),
        "thresholds": dict(thresholds),
        "gate_counts": dict(counts),
        "bound_candidate_ids": bound_candidates,
    }


def _statistical_decision_binding(
    *,
    statistics: Mapping[str, Any],
    decision: Mapping[str, Any],
    loop_report: Mapping[str, Any],
    candidate_audit: Mapping[str, Any],
    statistics_path: str | Path | None,
    decision_path: str | Path | None,
) -> dict[str, Any]:
    best = _optional_mapping(loop_report.get("best_candidate"))
    holdout = _optional_mapping(loop_report.get("holdout"))
    binding = _optional_mapping(candidate_audit.get("binding"))
    loop_id = str(loop_report.get("loop_id") or "")
    candidate_signature = str(best.get("signature") or "")
    holdout_run_id = str(holdout.get("portfolio_run_id") or "")
    model_candidate_id = str(binding.get("model_candidate_id") or "")

    statistics_body = _optional_mapping(statistics.get("statistics"))
    statistics_ledger = _optional_mapping(statistics.get("trial_ledger"))
    statistics_gates = _optional_mapping(statistics_body.get("binding_gates"))
    statistics_checks = _optional_mapping(statistics_gates.get("checks"))
    statistics_sample = _optional_mapping(
        statistics_body.get("sample_requirements")
    )
    required_sessions = _nonnegative_integer(
        statistics_sample.get("required_sessions")
    )
    required_trades = _nonnegative_integer(
        statistics_sample.get("required_trades")
    )
    sharpe_interval = _optional_mapping(
        _optional_mapping(
            statistics_body.get("bootstrap_confidence_interval")
        ).get("annualized_sharpe")
    )
    sharpe_lower = _finite_number(sharpe_interval.get("lower"))
    dsr = _optional_mapping(statistics_body.get("deflated_sharpe_ratio"))
    dsr_p_value = _finite_number(dsr.get("p_value"))
    target_power = _optional_mapping(statistics_body.get("target_power"))
    n_eff_measured = _finite_number(target_power.get("N_eff_measured"))
    n_eff_required = _finite_number(target_power.get("N_eff_required"))
    decision_checks = _optional_mapping(decision.get("checks"))
    decision_ledger = _optional_mapping(decision.get("trial_ledger"))

    statistics_file_exists = bool(
        statistics_path is not None
        and Path(statistics_path).resolve(strict=False).is_file()
    )
    decision_file_exists = bool(
        decision_path is not None
        and Path(decision_path).resolve(strict=False).is_file()
    )
    statistics_ledger_hash = str(statistics_ledger.get("ledger_hash") or "")
    decision_ledger_hash = str(decision_ledger.get("ledger_hash") or "")
    statistics_n_trials = _nonnegative_integer(
        statistics_ledger.get("N_trials_total")
    )
    checks = {
        "statistics_artifact_present": bool(statistics and statistics_file_exists),
        "statistics_type_matches": statistics.get("artifact_type")
        == "kinace_v3_research_statistics",
        "statistics_loop_matches": str(statistics.get("loop_id") or "") == loop_id,
        "statistics_candidate_matches": bool(
            model_candidate_id
            and str(statistics.get("candidate_id") or "") == model_candidate_id
        ),
        "statistics_holdout_run_matches": str(statistics.get("run_id") or "")
        == holdout_run_id,
        "statistics_trial_ledger_accounting_complete": bool(
            statistics_ledger.get("ledger_accounting_complete") is True
            and statistics_n_trials is not None
            and statistics_n_trials > 0
        ),
        "statistics_bootstrap_confidence_interval_computable": bool(
            statistics_checks.get("bootstrap_confidence_interval_computable")
            is True
            and sharpe_lower is not None
        ),
        "statistics_bootstrap_ci_lower_positive": bool(
            sharpe_lower is not None and sharpe_lower > 0
        ),
        "statistics_dsr_computable": bool(dsr.get("computable") is True),
        "statistics_dsr_p_value_below_0_05": bool(
            dsr.get("computable") is True
            and dsr_p_value is not None
            and dsr_p_value < 0.05
        ),
        "statistics_N_eff_target_met": bool(
            statistics_checks.get("effective_sample_size_meets_target") is True
            and n_eff_measured is not None
            and n_eff_required is not None
            and n_eff_measured >= n_eff_required
        ),
        "statistics_minimum_sessions_established": statistics_checks.get(
            "minimum_observation_sessions_established"
        )
        is True,
        "statistics_minimum_sessions_threshold_positive": bool(
            required_sessions is not None and required_sessions > 0
        ),
        "statistics_minimum_sessions_met": bool(
            statistics_checks.get("minimum_observation_sessions_met") is True
            and statistics_sample.get("sessions_met") is True
        ),
        "statistics_minimum_trades_established": statistics_checks.get(
            "minimum_trades_established"
        )
        is True,
        "statistics_minimum_trades_threshold_positive": bool(
            required_trades is not None and required_trades > 0
        ),
        "statistics_minimum_trades_met": bool(
            statistics_checks.get("minimum_trades_met") is True
            and statistics_sample.get("trades_met") is True
        ),
        "statistics_binding_gates_passed": bool(
            statistics_gates.get("passed") is True
            and statistics_checks
            and all(bool(value) for value in statistics_checks.values())
        ),
        "statistics_phase_1b_ready": statistics.get("phase_1b_ready") is True,
        "decision_artifact_present": bool(decision and decision_file_exists),
        "decision_type_matches": decision.get("artifact_type")
        == "kinace_v3_provisional_phase1b_decision",
        "decision_loop_matches": str(decision.get("loop_id") or "") == loop_id,
        "decision_candidate_matches": str(
            decision.get("candidate_signature") or ""
        )
        == candidate_signature,
        "decision_trial_ledger_matches_statistics": bool(
            decision_ledger_hash
            and decision_ledger_hash == statistics_ledger_hash
        ),
        "decision_status_passed": decision.get("status")
        == "provisional_passed",
        "decision_all_checks_passed": bool(
            decision_checks and all(bool(value) for value in decision_checks.values())
        ),
        "candidate_audit_passed": bool(
            candidate_audit.get("candidate_audit_passed") is True
            and candidate_audit.get("status") == "passed"
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "evidence": {
            "N_trials_total": statistics_ledger.get("N_trials_total"),
            "N_trials_total_recorded": statistics_ledger.get(
                "N_trials_total_recorded"
            ),
            "unrecorded_trials_upper_bound": statistics_ledger.get(
                "unrecorded_trials_upper_bound"
            ),
            "ledger_complete": statistics_ledger.get("ledger_complete"),
            "bootstrap_ci_lower": sharpe_lower,
            "dsr_computable": dsr.get("computable"),
            "dsr_p_value": dsr_p_value,
            "N_eff_measured": n_eff_measured,
            "N_eff_required": n_eff_required,
            "decision_status": decision.get("status"),
        },
        "statistics_path": (
            str(Path(statistics_path).resolve(strict=False))
            if statistics_path is not None
            else None
        ),
        "decision_path": (
            str(Path(decision_path).resolve(strict=False))
            if decision_path is not None
            else None
        ),
        "statistics_sha256": _sha256_if_file(statistics_path),
        "decision_sha256": _sha256_if_file(decision_path),
    }


def run_requalification(
    database: str | Path | None,
    *,
    loop_report_path: str | Path,
    preflight_path: str | Path,
    candidate_audit_path: str | Path,
    release_gate_path: str | Path,
    statistics_path: str | Path | None = None,
    decision_path: str | Path | None = None,
    output_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
) -> dict[str, Any]:
    target = database_path() if database is None else validate_database_path(database)
    loop_report = _mapping(_load_json(loop_report_path, "loop report"), "loop report")
    preflight = _mapping(_load_json(preflight_path, "preflight audit"), "preflight audit")
    candidate_audit = _mapping(
        _load_json(candidate_audit_path, "candidate audit"), "candidate audit"
    )
    release_gate = _mapping(
        _load_json(release_gate_path, "release gate"), "release gate"
    )
    statistics = (
        _mapping(_load_json(statistics_path, "research statistics"), "research statistics")
        if statistics_path is not None
        else {}
    )
    decision = (
        _mapping(
            _load_json(decision_path, "provisional decision"),
            "provisional decision",
        )
        if decision_path is not None
        else {}
    )
    loop_id = str(loop_report.get("loop_id") or "")
    statistical_decision_binding = _statistical_decision_binding(
        statistics=statistics,
        decision=decision,
        loop_report=loop_report,
        candidate_audit=candidate_audit,
        statistics_path=statistics_path,
        decision_path=decision_path,
    )
    with _connect(target) as connection:
        d1_checks, fold_checks = _d1_checks(
            connection,
            loop_report_path=loop_report_path,
            loop_report=loop_report,
            candidate_audit=candidate_audit,
        )
        d5 = _d5_payload(
            connection,
            preflight=preflight,
            candidate_audit=candidate_audit,
            loop_id=loop_id,
        )
        d3 = _d3_payload(
            connection,
            release_gate=release_gate,
            loop_report=loop_report,
            candidate_audit=candidate_audit,
        )
    d1 = {
        "status": "passed" if all(d1_checks.values()) else "failed",
        "checks": d1_checks,
        "fold_checks": fold_checks,
    }
    overall = (
        "passed"
        if d1["status"] == "passed"
        and statistical_decision_binding["status"] == "passed"
        and d3["status"] == "passed"
        and d5["status"] == "passed"
        else "failed"
    )
    identity = {
        "database": str(target),
        "loop_id": loop_id,
        "candidate_audit_id": candidate_audit.get("audit_id"),
        "release_gate": release_gate.get("generated_at_utc"),
        "statistics_sha256": statistical_decision_binding["statistics_sha256"],
        "decision_sha256": statistical_decision_binding["decision_sha256"],
    }
    qualification_id = sha256_bytes(
        canonical_json_bytes(identity, trailing_newline=False)
    )
    target_output = (
        Path(output_path).resolve(strict=False)
        if output_path
        else Path(loop_report_path).resolve(strict=False).with_name(
            "D1-D3-D5-requalification.json"
        )
    )
    markdown_target = (
        Path(markdown_path).resolve(strict=False)
        if markdown_path
        else target_output.with_suffix(".md")
    )
    payload = {
        "artifact_type": "kinace_v322_d1_d3_d5_requalification",
        "schema_version": SCHEMA_VERSION,
        "qualification_id": qualification_id,
        "generated_at_utc": _utc_now(),
        "database": str(target),
        "database_sha256": sha256_file(target),
        "loop_id": loop_id,
        "candidate_signature": (
            _mapping(loop_report.get("best_candidate"), "best_candidate") or {}
        ).get("signature"),
        "holdout_run_id": (
            _mapping(loop_report.get("holdout"), "holdout") or {}
        ).get("portfolio_run_id"),
        "d1": d1,
        "d2": statistical_decision_binding,
        "d3": d3,
        "d5": d5,
        "statistical_decision_binding": statistical_decision_binding,
        "overall_status": overall,
        "production_changed": False,
    }
    lines = [
        "# Kinace V3.3 D1-D3-D5 Requalification",
        "",
        f"- Qualification ID: `{qualification_id}`",
        f"- Generated: `{payload['generated_at_utc']}`",
        f"- Overall: `{overall}`",
        f"- Database: `{target}`",
        f"- Loop: `{loop_id}`",
        "",
        "| Gate | Status |",
        "|---|---|",
        f"| D1 | `{d1['status']}` |",
        f"| D2 | `{statistical_decision_binding['status']}` |",
        f"| D3 | `{d3['status']}` |",
        f"| D5 | `{d5['status']}` |",
        f"| Overall | `{overall}` |",
    ]
    _write_text("\n".join(lines) + "\n", markdown_target)
    target_output.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_output.with_suffix(target_output.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(target_output)
    payload["artifact_path"] = str(target_output)
    payload["markdown_path"] = str(markdown_target)
    payload["artifact_sha256"] = sha256_file(target_output)
    return payload


__all__ = [
    "ResearchQualificationError",
    "run_requalification",
]

