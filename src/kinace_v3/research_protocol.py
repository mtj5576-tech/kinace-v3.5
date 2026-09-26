"""Freeze effective research configuration, code, and runtime lineage."""

from __future__ import annotations

import hashlib
import os
import platform
import sqlite3
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .config import PROJECT_ROOT, validate_database_path
from .feature_snapshot import canonical_json_bytes, sha256_bytes
from .history_features import DEFAULT_MODEL_CONFIG
from .optimization import DEFAULT_OBJECTIVE, normalize_objective_config
from .research_holdout import (
    DEFAULT_HOLDOUT_EXTENSION_POLICY,
    holdout_extension_policy_id,
    normalize_holdout_extension_policy,
)
from .research_stability import (
    CANDIDATE_TRADE_COUNT_SCOPE,
    DEFAULT_STABILITY_CONFIG,
    TRADE_COUNT_POLICY_VERSION,
)
from .trial_backtest import (
    DEFAULT_CONFIG as DEFAULT_BACKTEST_CONFIG,
    MARK_TO_MARKET_EQUITY_BASIS,
    RETURN_ANOMALY_POLICY,
)


SCHEMA_VERSION = 2
EVALUATION_PROTOCOL_HASH_FILENAME = "evaluation_protocol_hash.txt"
EFFECTIVE_PROTOCOL_FILENAME = "effective_research_protocol.json"
CANDIDATE_MANIFEST_FILENAME = "candidate_config_manifest.json"
ACTIVE_CHALLENGER_STAGES = frozenset(
    {
        "challenger",
        "challenger_ready",
        "awaiting_candidate_audit",
        "audit_passed_candidate",
    }
)
LEGACY_REQUIRED_PROTOCOL_FIELDS = (
    "purge_sessions",
    "embargo_sessions",
    "label_horizon_sessions",
    "max_feature_weight",
    "min_trade_count",
    "max_drawdown",
    "turnover_budget",
    "max_weight_report_only",
    "max_weight_binding",
    "max_weight_budget",
    "objective_policy_version",
)
HOLDOUT_EXTENSION_REQUIRED_FIELDS = (
    "holdout_extension_policy_id",
    "holdout_initial_sessions",
    "holdout_min_trade_count",
    "holdout_max_total_sessions",
)
SAMPLE_REQUIREMENT_REQUIRED_FIELDS = ("min_observation_sessions",)
REQUIRED_PROTOCOL_FIELDS = (
    LEGACY_REQUIRED_PROTOCOL_FIELDS
    + SAMPLE_REQUIREMENT_REQUIRED_FIELDS
    + HOLDOUT_EXTENSION_REQUIRED_FIELDS
)
LABEL_SEMANTICS: dict[str, Any] = {
    "name": "forward_return_5d",
    "horizon_sessions": 5,
    "available_horizons_sessions": [1, 3, 5],
    "primary_horizon_sessions": 5,
    "decision_date_field": "decision_date",
    "entry_date_field": "next_trade_date",
    "entry_price_field": "next_open",
    "exit_price_field": "close_at_primary_horizon",
    "formula": "close_{t+5} / open_{t+1} - 1",
    "price_basis": "total_return",
    "source": "data_collection.models.ForwardLabelRecord",
}
EXECUTION_SEMANTICS: dict[str, Any] = {
    "signal_date": "decision_date",
    "entry_timing": "next_trading_day_open",
    "exit_timing": "session_close_or_rule_trigger",
    "price_basis": "raw",
    "execution_price_basis": "raw",
    "research_price_basis": "total_return",
    "feature_price_basis": "total_return",
    "label_price_basis": "total_return",
    "benchmark_price_basis": "total_return",
    "tradability_price_basis": "raw",
    "limits_price_basis": "raw",
    "lot_sizing_price_basis": "raw",
    "fees_price_basis": "raw",
    "fill_validation_price_basis": "raw",
    "min_close_price_basis": "raw",
    "intended_fill_field": "open",
    "slippage_direction": {"buy": "positive", "sell": "negative"},
    "equity_basis": MARK_TO_MARKET_EQUITY_BASIS,
    "equity_curve": "mark_to_market_equity",
    "return_anomaly_policy": deepcopy(RETURN_ANOMALY_POLICY),
}


class ResearchProtocolError(ValueError):
    """Raised when Phase 0 protocol lineage cannot be frozen safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchProtocolError(f"{field} must be an object")
    return value


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


def _sha256_file_or_none(path: str | Path | None) -> str | None:
    if not path:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit_sha(project_root: Path) -> str:
    configured = os.environ.get("KINACE_CODE_COMMIT_SHA", "").strip()
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def _source_tree_sha256(project_root: Path) -> str:
    roots = (
        project_root / "src" / "kinace_v3",
        project_root / "config",
    )
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix in {".py", ".json"}
            )
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        files.append(pyproject)
    entries = []
    for path in sorted(set(files), key=lambda value: str(value).casefold()):
        relative = path.relative_to(project_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append({"path": relative, "sha256": digest})
    if not entries:
        return "unavailable"
    return sha256_bytes(canonical_json_bytes(entries, trailing_newline=False))


def code_lineage(code_version: str) -> dict[str, Any]:
    """Return deterministic code lineage for the current working tree."""
    return {
        "code_version": str(code_version),
        "package_version": str(__version__),
        "code_commit_sha": _git_commit_sha(PROJECT_ROOT),
        "source_tree_sha256": _source_tree_sha256(PROJECT_ROOT),
    }


def runtime_lineage() -> dict[str, Any]:
    """Return the deterministic runtime assumptions used by the core package."""
    declaration_path = PROJECT_ROOT / "pyproject.toml"
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "runtime_platform": platform.system(),
        "runtime_machine": platform.machine(),
        "thread_count": 1,
        "blas_backend": "stdlib_only",
        "device": "cpu",
        "dependency_lock_hash": "stdlib_only",
        "dependency_declaration_sha256": _sha256_file_or_none(declaration_path)
        or "unavailable",
        "determinism_flags": ["single_thread", "stdlib_only"],
    }


def _normalize_stability(
    stability_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = dict(stability_config or {})
    unknown = sorted(set(source) - set(DEFAULT_STABILITY_CONFIG))
    if unknown:
        raise ResearchProtocolError(
            "unknown stability configuration keys: " + ", ".join(unknown)
        )
    result = deepcopy(DEFAULT_STABILITY_CONFIG)
    result.update(source)
    result["min_oos_windows"] = int(result["min_oos_windows"])
    result["candidate_min_trade_count"] = int(
        result["candidate_min_trade_count"]
    )
    result["fold_min_trade_count"] = int(result["fold_min_trade_count"])
    if not isinstance(result["max_feature_weight_binding"], bool):
        raise ResearchProtocolError(
            "stability.max_feature_weight_binding must be boolean"
        )
    for key in (
        "min_noninferior_rate",
        "min_candidate_gate_pass_rate",
        "max_drawdown_regression",
        "max_feature_weight",
        "cost_min_positive_rate",
        "market_filter_min_positive_rate",
        "market_filter_max_return_range",
        "min_active_fold_rate",
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResearchProtocolError(f"stability.{key} must be numeric")
        result[key] = float(value)
    slippage = result["cost_slippage_bps"]
    if not isinstance(slippage, Sequence) or isinstance(slippage, (str, bytes)):
        raise ResearchProtocolError("stability.cost_slippage_bps must be a list")
    values = sorted({float(value) for value in slippage})
    if not values or values[0] < 0:
        raise ResearchProtocolError(
            "stability.cost_slippage_bps must contain non-negative values"
        )
    result["cost_slippage_bps"] = values
    if result["candidate_min_trade_count"] <= 0:
        raise ResearchProtocolError(
            "stability.candidate_min_trade_count must be positive"
        )
    if result["fold_min_trade_count"] <= 0:
        raise ResearchProtocolError(
            "stability.fold_min_trade_count must be positive"
        )
    if not 0 < result["min_active_fold_rate"] <= 1:
        raise ResearchProtocolError(
            "stability.min_active_fold_rate must be in (0, 1]"
        )
    if result["trade_count_policy_version"] != TRADE_COUNT_POLICY_VERSION:
        raise ResearchProtocolError(
            "unsupported stability.trade_count_policy_version"
        )
    if result["trade_count_scope"] != CANDIDATE_TRADE_COUNT_SCOPE:
        raise ResearchProtocolError("unsupported stability.trade_count_scope")
    return result


def _evaluation_policy(source: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(source or {})
    unknown = sorted(set(payload) - {"target_sharpe"})
    if unknown:
        raise ResearchProtocolError(
            "unknown evaluation policy keys: " + ", ".join(unknown)
        )
    target = payload.get("target_sharpe")
    if target is not None:
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise ResearchProtocolError("evaluation_policy.target_sharpe must be numeric")
        target = float(target)
        if target <= 0:
            raise ResearchProtocolError("evaluation_policy.target_sharpe must be positive")
    return {
        "target_sharpe": target,
        "target_sharpe_frozen": target is not None,
    }


def _candidate_entries(seeds: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "signature": str(seed.get("signature") or ""),
            "model_config": deepcopy(
                dict(_mapping(seed.get("model_config"), "seed.model_config"))
            ),
            "backtest_config": deepcopy(
                dict(_mapping(seed.get("backtest_config"), "seed.backtest_config"))
            ),
        }
        for seed in seeds
    ]


def build_effective_research_protocol(
    *,
    loop_id: str,
    code_version: str,
    objective: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
    stability_config: Mapping[str, Any] | None,
    seeds: Sequence[Mapping[str, Any]],
    model_search_space: Mapping[str, Any],
    backtest_search_space: Mapping[str, Any],
    walk_forward_defaults: Mapping[str, Any],
    evaluation_policy: Mapping[str, Any] | None = None,
    holdout_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic evaluation protocol and its audit wrapper."""
    stability = _normalize_stability(stability_config)
    holdout = normalize_holdout_extension_policy(holdout_policy)
    policy = _evaluation_policy(evaluation_policy)
    objective = normalize_objective_config(objective)
    candidates = _candidate_entries(seeds)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "code_lineage": code_lineage(code_version),
        "runtime_lineage": runtime_lineage(),
        "evaluation_config": {
            "objective": deepcopy(dict(objective)),
            "walk_forward": deepcopy(dict(walk_forward)),
            "stability": stability,
            "evaluation_policy": policy,
            "holdout_policy": holdout,
            "label_semantics": deepcopy(LABEL_SEMANTICS),
            "execution_semantics": deepcopy(EXECUTION_SEMANTICS),
        },
    }
    protocol_hash = sha256_bytes(
        canonical_json_bytes(identity, trailing_newline=False)
    )
    required_fields = {
        "purge_sessions": int(walk_forward["purge_sessions"]),
        "embargo_sessions": int(walk_forward["embargo_sessions"]),
        "label_horizon_sessions": int(LABEL_SEMANTICS["horizon_sessions"]),
        "max_feature_weight": float(stability["max_feature_weight"]),
        "min_observation_sessions": int(
            objective["constraints"]["min_observation_sessions"]["threshold"]
        ),
        "min_trade_count": int(
            objective["constraints"]["min_trade_count"]["threshold"]
        ),
        "max_drawdown": float(
            objective["constraints"]["max_drawdown"]["threshold"]
        ),
        "turnover_budget": float(
            objective["constraints"]["max_turnover_per_session"]["threshold"]
        ),
        "max_weight_report_only": not bool(
            stability["max_feature_weight_binding"]
        ),
        "max_weight_binding": bool(stability["max_feature_weight_binding"]),
        "max_weight_budget": float(stability["max_feature_weight"]),
        "objective_policy_version": str(objective["policy"]["version"]),
        "holdout_extension_policy_id": holdout_extension_policy_id(holdout),
        "holdout_initial_sessions": int(holdout["initial_holdout_sessions"]),
        "holdout_min_trade_count": int(holdout["min_trade_count"]),
        "holdout_max_total_sessions": int(holdout["max_total_sessions"]),
    }
    return {
        "artifact_type": "kinace_v3_effective_research_protocol",
        "schema_version": SCHEMA_VERSION,
        "loop_id": str(loop_id),
        "code_version": str(code_version),
        "evaluation_protocol_hash": protocol_hash,
        "protocol_identity": identity,
        "configuration_lineage": {
            "code_default": {
                "model_config": deepcopy(DEFAULT_MODEL_CONFIG),
                "backtest_config": deepcopy(DEFAULT_BACKTEST_CONFIG),
                "objective": deepcopy(DEFAULT_OBJECTIVE),
                "walk_forward": deepcopy(dict(walk_forward_defaults)),
                "stability": deepcopy(DEFAULT_STABILITY_CONFIG),
                "holdout_policy": deepcopy(DEFAULT_HOLDOUT_EXTENSION_POLICY),
            },
            "active_config": {
                "objective": deepcopy(dict(objective)),
                "walk_forward": deepcopy(dict(walk_forward)),
                "stability": stability,
                "evaluation_policy": policy,
                "holdout_policy": holdout,
                "model_search_space": deepcopy(dict(model_search_space)),
                "backtest_search_space": deepcopy(dict(backtest_search_space)),
                "seed_candidates": candidates,
            },
            "effective_runtime": {
                "objective": deepcopy(dict(objective)),
                "walk_forward": deepcopy(dict(walk_forward)),
                "stability": stability,
                "evaluation_policy": policy,
                "holdout_policy": holdout,
                "model_search_space": deepcopy(dict(model_search_space)),
                "backtest_search_space": deepcopy(dict(backtest_search_space)),
                "seed_candidate_signatures": [
                    item["signature"] for item in candidates
                ],
                "scheduled_candidate_count": len(candidates),
                "scheduled_round_count": int(walk_forward["max_rounds"]),
            },
        },
        "required_evaluation_fields": required_fields,
        "production_ready": False,
        "production_changed": False,
        "frozen_at_utc": _utc_now(),
    }


def recompute_evaluation_protocol_hash(protocol: Mapping[str, Any]) -> str:
    """Recompute the hash from a protocol wrapper or raw protocol identity."""
    source = dict(_mapping(protocol, "evaluation protocol"))
    identity = source.get("protocol_identity", source)
    return sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))


def protocol_candidate_id(protocol_hash: str, candidate_signature: str) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "evaluation_protocol_hash": str(protocol_hash),
                "candidate_signature": str(candidate_signature),
            },
            trailing_newline=False,
        )
    )


def required_evaluation_fields(protocol: Mapping[str, Any]) -> dict[str, Any]:
    fields = protocol.get("required_evaluation_fields")
    if not isinstance(fields, Mapping):
        raise ResearchProtocolError("required_evaluation_fields must be an object")
    names = required_protocol_field_names(protocol)
    missing = [name for name in names if name not in fields]
    if missing:
        raise ResearchProtocolError(
            "required evaluation fields are missing: " + ", ".join(missing)
        )
    return {name: fields[name] for name in names}


def required_protocol_field_names(protocol: Mapping[str, Any]) -> tuple[str, ...]:
    version = protocol.get("schema_version")
    if isinstance(version, int) and version >= 2:
        return REQUIRED_PROTOCOL_FIELDS
    return LEGACY_REQUIRED_PROTOCOL_FIELDS


def build_candidate_config_manifest(
    *,
    loop_id: str,
    evaluation_protocol_hash: str,
    seed_candidates: Sequence[Mapping[str, Any]],
    model_search_space: Mapping[str, Any],
    backtest_search_space: Mapping[str, Any],
    rounds: Sequence[Mapping[str, Any]],
    observed_candidate_count: int,
) -> dict[str, Any]:
    """Build a candidate-level manifest bound to the frozen protocol hash."""
    planned = []
    for seed in seed_candidates:
        signature = str(seed.get("signature") or "")
        planned.append(
            {
                "signature": signature,
                "protocol_candidate_id": protocol_candidate_id(
                    evaluation_protocol_hash, signature
                ),
                "model_config": deepcopy(
                    dict(_mapping(seed.get("model_config"), "seed.model_config"))
                ),
                "backtest_config": deepcopy(
                    dict(_mapping(seed.get("backtest_config"), "seed.backtest_config"))
                ),
                "origin": "seed",
            }
        )
    observed = []
    for round_payload in rounds:
        round_number = int(round_payload.get("round") or 0)
        for candidate in round_payload.get("candidates") or []:
            mapping = _mapping(candidate, "round candidate")
            signature = str(mapping.get("signature") or "")
            validation = mapping.get("validation")
            validation = validation if isinstance(validation, Mapping) else {}
            observed.append(
                {
                    "signature": signature,
                    "protocol_candidate_id": protocol_candidate_id(
                        evaluation_protocol_hash, signature
                    ),
                    "round": round_number,
                    "validation_status": str(validation.get("status") or ""),
                    "model_config": deepcopy(
                        dict(
                            _mapping(
                                mapping.get("model_config"),
                                "candidate.model_config",
                            )
                        )
                    ),
                    "backtest_config": deepcopy(
                        dict(
                            _mapping(
                                mapping.get("backtest_config"),
                                "candidate.backtest_config",
                            )
                        )
                    ),
                }
            )
    return {
        "artifact_type": "kinace_v3_candidate_config_manifest",
        "schema_version": SCHEMA_VERSION,
        "loop_id": str(loop_id),
        "evaluation_protocol_hash": str(evaluation_protocol_hash),
        "planned_candidates": planned,
        "model_search_space": deepcopy(dict(model_search_space)),
        "backtest_search_space": deepcopy(dict(backtest_search_space)),
        "rounds": [
            {
                "round": int(item.get("round") or 0),
                "candidate_count": int(item.get("candidate_count") or 0),
                "best_candidate_signature": item.get("best_candidate_signature"),
                "candidate_signatures": [
                    str(candidate.get("signature") or "")
                    for candidate in item.get("candidates") or []
                    if isinstance(candidate, Mapping)
                ],
            }
            for item in rounds
        ],
        "observed_execution": {
            "round_count": len(rounds),
            "candidate_count": int(observed_candidate_count),
            "candidate_signatures": sorted(
                {item["signature"] for item in observed if item["signature"]}
            ),
        },
        "observed_candidates": observed,
        "production_ready": False,
        "production_changed": False,
        "created_at_utc": _utc_now(),
    }


def write_effective_protocol(
    protocol: Mapping[str, Any],
    loop_dir: str | Path,
) -> dict[str, Any]:
    target_dir = Path(loop_dir).resolve(strict=False)
    protocol_path = _write_json(protocol, target_dir / EFFECTIVE_PROTOCOL_FILENAME)
    hash_path = _write_text(
        str(protocol["evaluation_protocol_hash"]) + "\n",
        target_dir / EVALUATION_PROTOCOL_HASH_FILENAME,
    )
    return {
        "evaluation_protocol_hash": str(protocol["evaluation_protocol_hash"]),
        "effective_protocol_path": str(protocol_path),
        "effective_protocol_sha256": _sha256_file_or_none(protocol_path),
        "hash_path": str(hash_path),
        "hash_sha256": _sha256_file_or_none(hash_path),
    }


def write_candidate_manifest(
    manifest: Mapping[str, Any],
    loop_dir: str | Path,
) -> dict[str, Any]:
    target = _write_json(
        manifest,
        Path(loop_dir).resolve(strict=False) / CANDIDATE_MANIFEST_FILENAME,
    )
    return {
        "candidate_manifest_path": str(target),
        "candidate_manifest_sha256": _sha256_file_or_none(target),
    }


def archive_active_challengers(
    database: str | Path,
    *,
    superseded_by_loop_id: str,
    reason: str = "V3.3 Phase 0 protocol freeze",
) -> dict[str, Any]:
    """Move active challenger states to an explicit superseded terminal state."""
    target = validate_database_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(target)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "research_candidates" not in tables:
            return {
                "status": "not_applicable",
                "reason": "research_candidates table is absent",
                "superseded_candidates": [],
            }
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(research_candidates)")
        }
        required_columns = {
            "superseded_by_loop_id",
            "superseded_reason",
            "superseded_at_utc",
        }
        if not required_columns <= columns:
            return {
                "status": "not_applicable",
                "reason": "research_candidates archive columns are absent",
                "superseded_candidates": [],
            }
        placeholders = ", ".join("?" for _ in ACTIVE_CHALLENGER_STAGES)
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT candidate_signature, stage, latest_loop_id, holdout_status
                FROM research_candidates
                WHERE stage IN ({placeholders})
                ORDER BY updated_at_utc, candidate_signature
                """,
                tuple(sorted(ACTIVE_CHALLENGER_STAGES)),
            )
        ]
        archived_at = _utc_now()
        if rows:
            connection.executemany(
                """
                UPDATE research_candidates
                SET stage='superseded',
                    superseded_by_loop_id=?,
                    superseded_reason=?,
                    superseded_at_utc=?,
                    updated_at_utc=?
                WHERE candidate_signature=?
                """,
                [
                    (
                        str(superseded_by_loop_id),
                        str(reason),
                        archived_at,
                        archived_at,
                        row["candidate_signature"],
                    )
                    for row in rows
                ],
            )
            connection.commit()
        return {
            "status": "archived" if rows else "no_active_challenger",
            "reason": str(reason),
            "superseded_by_loop_id": str(superseded_by_loop_id),
            "archived_at_utc": archived_at,
            "superseded_candidates": [
                {
                    **row,
                    "stage": "superseded",
                }
                for row in rows
            ],
        }


__all__ = [
    "ACTIVE_CHALLENGER_STAGES",
    "CANDIDATE_MANIFEST_FILENAME",
    "EFFECTIVE_PROTOCOL_FILENAME",
    "EVALUATION_PROTOCOL_HASH_FILENAME",
    "EXECUTION_SEMANTICS",
    "LABEL_SEMANTICS",
    "LEGACY_REQUIRED_PROTOCOL_FIELDS",
    "REQUIRED_PROTOCOL_FIELDS",
    "SAMPLE_REQUIREMENT_REQUIRED_FIELDS",
    "ResearchProtocolError",
    "SCHEMA_VERSION",
    "archive_active_challengers",
    "build_candidate_config_manifest",
    "build_effective_research_protocol",
    "code_lineage",
    "protocol_candidate_id",
    "recompute_evaluation_protocol_hash",
    "required_evaluation_fields",
    "required_protocol_field_names",
    "runtime_lineage",
    "write_candidate_manifest",
    "write_effective_protocol",
]
