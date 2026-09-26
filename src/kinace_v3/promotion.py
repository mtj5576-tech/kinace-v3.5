from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import PROJECT_ROOT, database_path, load_config, validate_database_path
from .reporting import DEFAULT_DATA_GATE_PATH, build_report


DEFAULT_PRODUCTION_PATH = PROJECT_ROOT / "config" / "model.production.v3.json"
PRODUCTION_CONFIG_ROOT = PROJECT_ROOT / "config"


class PromotionError(ValueError):
    """Raised when a promotion request violates the dry-run boundary."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.normcase(os.path.commonpath((str(path), str(root)))) == os.path.normcase(
            str(root)
        )
    except ValueError:
        return False


def validate_production_path(path: str | Path | None = None) -> Path:
    target = (
        DEFAULT_PRODUCTION_PATH
        if path is None
        else Path(os.path.expandvars(os.path.expanduser(os.fspath(path))))
    )
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target = target.resolve(strict=False)
    root = PRODUCTION_CONFIG_ROOT.resolve(strict=False)
    if not _is_within(target, root):
        raise PromotionError(f"production model path must stay under {root}: {target}")
    return target


def _read_candidate(
    connection: sqlite3.Connection,
    candidate_id: str | None,
) -> dict[str, Any] | None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "model_candidates" not in tables:
        return None
    if candidate_id is None:
        row = connection.execute(
            "SELECT * FROM model_candidates ORDER BY created_at_utc DESC LIMIT 1"
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM model_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ("candidate_config_json",):
        if isinstance(result.get(key), str):
            try:
                result[key.removesuffix("_json")] = json.loads(result.pop(key))
            except json.JSONDecodeError:
                pass
    return result


def _read_only_uri(path: Path) -> str:
    normalized = path.resolve(strict=False).as_posix()
    return f"file:{quote(normalized, safe='/:')}?mode=ro"


def build_promotion_dry_run(
    *,
    database: str | Path | None = None,
    candidate_id: str | None = None,
    experiment_id: str | None = None,
    run_id: str | None = None,
    production_path: str | Path | None = None,
    data_gate_path: str | Path = DEFAULT_DATA_GATE_PATH,
) -> dict[str, Any]:
    """Audit promotion inputs without exposing any production mutation path."""
    target = database_path(load_config()) if database is None else validate_database_path(database)
    resolved_production = validate_production_path(production_path)
    report = build_report(
        database=target,
        experiment_id=experiment_id,
        run_id=run_id,
        data_gate_path=data_gate_path,
    )

    candidate: dict[str, Any] | None = None
    if target.is_file():
        connection = sqlite3.connect(_read_only_uri(target), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            candidate = _read_candidate(connection, candidate_id)
        finally:
            connection.close()

    gate = report["data_gate"]
    gate_ready = bool(
        gate.get("ready")
        and not gate.get("development_only")
        and gate.get("production_eligible") is not False
    )
    gates = [
        {
            "name": "data_gate_ready",
            "passed": gate_ready,
            "detail": gate.get("status"),
        },
        {
            "name": "candidate_present",
            "passed": candidate is not None,
            "detail": candidate_id or (candidate or {}).get("candidate_id"),
        },
        {
            "name": "explicit_approval_path",
            "passed": False,
            "detail": "No production approval path is enabled in Kinace V3.",
        },
    ]
    if not gate_ready:
        status = "blocked_data_gate"
    elif candidate is None:
        status = "no_candidate"
    else:
        status = "blocked_approval_path_missing"

    return {
        "artifact_type": "kinace_v3_promotion_dry_run",
        "status": status,
        "dry_run": True,
        "production_changed": False,
        "mutation_allowed": False,
        "database": str(target),
        "candidate_id": candidate_id or (candidate or {}).get("candidate_id"),
        "experiment_id": experiment_id,
        "run_id": run_id,
        "production_path": str(resolved_production),
        "gates": gates,
        "report_status": report.get("status"),
        "note": (
            "Dry-run only. No production model file is written and no promotion "
            "is approved by this command."
        ),
    }


__all__ = [
    "DEFAULT_PRODUCTION_PATH",
    "PRODUCTION_CONFIG_ROOT",
    "PromotionError",
    "build_promotion_dry_run",
    "validate_production_path",
]
