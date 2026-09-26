"""Fail-closed path and storage isolation for synthetic research runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, PROTECTED_DATABASE_ROOTS, database_path, load_config
from .data_collection import RevisionStore
from .database import initialize_database
from .daily_run import initialize_daily_run_schema
from .feature_snapshot import canonical_json_bytes
from .formal_evidence import (
    DEFAULT_HOLDOUT_VAULT_PATH,
    DEFAULT_SNAPSHOT_DIR as DEFAULT_FORMAL_SNAPSHOT_DIR,
)


SCHEMA_VERSION = 1
TRACK = "sandbox"
FORMAL_TRACK = "formal"
MANIFEST_FILENAME = "sandbox-manifest.json"
LEDGER_FILENAME = "trial-ledger.jsonl"
HOLDOUT_REGISTRY_FILENAME = "holdout-access-registry.json"
DATABASE_FILENAME = "research.sqlite3"
VERIFICATION_FILENAME = "sandbox-verification.json"
DEFAULT_VERIFICATION_PATH = (
    PROJECT_ROOT / "reports" / "execution_framework" / "sandbox" / VERIFICATION_FILENAME
)
DEFAULT_FORMAL_TRIAL_LEDGER = PROJECT_ROOT / "reports" / "trial-ledger.jsonl"
SANDBOX_ROOT_ENV = "KINACE_SANDBOX_ROOT"
RUN_ID_PATTERN = re.compile(r"^sandbox-[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$", re.ASCII)

REQUIRED_FORMAL_SURFACES = (
    "database",
    "snapshots",
    "trial_ledger",
    "reports",
    "holdout_registry",
)
OWNED_SURFACES = (
    "database",
    "snapshots",
    "trial_ledger",
    "outputs",
    "reports",
    "holdout_registry",
    "run_ids",
    "manifest",
)


class SandboxError(ValueError):
    """Raised when a synthetic sandbox cannot be safely initialized or verified."""


class SandboxPathError(SandboxError):
    """Raised when a sandbox path overlaps or escapes a protected surface."""


def _resolve(path: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(path)))
    try:
        return Path(expanded).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SandboxPathError(f"cannot resolve sandbox path {expanded!r}: {exc}") from exc


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_within(path: Path, root: Path) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    if path_key == root_key:
        return True
    try:
        common = os.path.commonpath((path_key, root_key))
    except ValueError:
        return False
    return common == root_key


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _surface_paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "database": root / DATABASE_FILENAME,
        "snapshots": root / "snapshots",
        "trial_ledger": root / LEDGER_FILENAME,
        "outputs": root / "outputs",
        "reports": root / "reports",
        "holdout_registry": root / HOLDOUT_REGISTRY_FILENAME,
        "run_ids": root / "run-ids",
        "manifest": root / MANIFEST_FILENAME,
    }


def resolve_sandbox_paths(
    *,
    root: str | os.PathLike[str],
    formal_paths: Mapping[str, str | os.PathLike[str]],
    protected_roots: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, Path]:
    """Resolve one sandbox root and reject every protected-path overlap.

    Comparison uses fully resolved paths so relative traversal, symlink aliases,
    and Windows case aliases cannot bypass the boundary. This function performs
    no filesystem writes.
    """
    if not isinstance(formal_paths, Mapping):
        raise SandboxPathError("formal_paths must be a mapping")
    missing = [name for name in REQUIRED_FORMAL_SURFACES if not formal_paths.get(name)]
    if missing:
        raise SandboxPathError(
            "formal_paths is missing required surfaces: " + ", ".join(missing)
        )

    resolved_root = _resolve(root)
    if not str(resolved_root).strip():
        raise SandboxPathError("sandbox root must be explicit")

    formal = {
        str(name): _resolve(value)
        for name, value in formal_paths.items()
        if value is not None and str(value).strip()
    }
    protected = tuple(_resolve(value) for value in protected_roots if str(value).strip())
    surfaces = _surface_paths(resolved_root)

    resolved_surfaces: dict[str, Path] = {}
    for surface_name, surface_path in surfaces.items():
        resolved_surface = _resolve(surface_path)
        resolved_surfaces[surface_name] = resolved_surface
        if surface_name != "root" and not _is_within(resolved_surface, resolved_root):
            raise SandboxPathError(
                f"sandbox surface escapes the explicit root: "
                f"{surface_name}={resolved_surface}"
            )
        for formal_name, formal_path in formal.items():
            if _paths_overlap(resolved_surface, formal_path):
                raise SandboxPathError(
                    "sandbox path overlaps or contains a formal path: "
                    f"{surface_name}={resolved_surface}, "
                    f"formal_{formal_name}={formal_path}"
                )
        for protected_root in protected:
            if _paths_overlap(resolved_surface, protected_root):
                raise SandboxPathError(
                    "sandbox path overlaps or contains a protected database root: "
                    f"{surface_name}={resolved_surface}, protected={protected_root}"
                )

    return resolved_surfaces


def configured_formal_paths(
    config_path: str | os.PathLike[str] | None = None,
) -> dict[str, Path]:
    """Return the formal surfaces used by the sandbox isolation resolver."""
    config_file = _resolve(
        config_path
        or os.environ.get("KINACE_V32_CONFIG")
        or PROJECT_ROOT / "config" / "config.v3.json"
    )
    config = load_config(config_file)
    return {
        "database": database_path(config),
        "snapshots": _resolve(DEFAULT_FORMAL_SNAPSHOT_DIR),
        "trial_ledger": _resolve(DEFAULT_FORMAL_TRIAL_LEDGER),
        "reports": _resolve(PROJECT_ROOT / "reports"),
        "holdout_registry": _resolve(DEFAULT_HOLDOUT_VAULT_PATH),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    temporary.write_bytes(payload)
    temporary.replace(target)


def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    _write_bytes_atomic(path, canonical_json_bytes(payload))
    return _resolve(path)


def _sandbox_identity(root: Path) -> str:
    return hashlib.sha256(_path_key(root).encode("utf-8")).hexdigest()


def sandbox_run_id(
    root: str | os.PathLike[str],
    *,
    purpose: str,
    identity: Mapping[str, Any] | str | int | float | bool | None = None,
) -> str:
    """Return a deterministic sandbox-only run ID with a stable root namespace."""
    label = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in str(purpose).strip().lower()
    ).strip("-")
    if not label:
        raise SandboxError("sandbox run purpose must be a non-empty string")
    digest = hashlib.sha256(_path_key(_resolve(root)).encode("utf-8"))
    if identity is not None:
        digest.update(canonical_json_bytes({"identity": identity}, trailing_newline=False))
    return f"sandbox-{label}-{digest.hexdigest()[:24]}"


def _initial_registry() -> dict[str, Any]:
    return {
        "artifact_type": "kinace_v3_holdout_access_registry",
        "schema_version": 1,
        "track": TRACK,
        "access_count": 0,
        "entries": [],
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SandboxError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError(f"cannot load {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SandboxError(f"{label} must be a JSON object")
    return payload


def _manifest_payload(
    layout: Mapping[str, Path],
    *,
    formal_paths: Mapping[str, str | os.PathLike[str]],
    protected_roots: Sequence[str | os.PathLike[str]],
) -> dict[str, Any]:
    root = layout["root"]
    identity = _sandbox_identity(root)
    return {
        "artifact_type": "kinace_v3_synthetic_sandbox",
        "schema_version": SCHEMA_VERSION,
        "track": TRACK,
        "status": "initialized",
        "root": str(root),
        "sandbox_id": identity,
        "run_id_prefix": f"sandbox-{identity[:24]}",
        "owned_paths": {name: str(layout[name]) for name in OWNED_SURFACES},
        "formal_paths": {name: str(_resolve(path)) for name, path in formal_paths.items()},
        "protected_database_roots": [str(_resolve(path)) for path in protected_roots],
        "created_at_utc": _now(),
    }


def initialize_sandbox(
    *,
    root: str | os.PathLike[str],
    formal_paths: Mapping[str, str | os.PathLike[str]] | None = None,
    protected_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> dict[str, Any]:
    """Create all sandbox-owned stores after validating the full path boundary."""
    resolved_formal = formal_paths or configured_formal_paths()
    resolved_protected = tuple(
        PROTECTED_DATABASE_ROOTS if protected_roots is None else protected_roots
    )
    layout = resolve_sandbox_paths(
        root=root,
        formal_paths=resolved_formal,
        protected_roots=resolved_protected,
    )

    sandbox_root = layout["root"]
    if sandbox_root.exists() and not sandbox_root.is_dir():
        raise SandboxError(f"sandbox root is not a directory: {sandbox_root}")
    sandbox_root.mkdir(parents=True, exist_ok=True)
    for name in ("snapshots", "outputs", "reports", "run_ids"):
        layout[name].mkdir(parents=True, exist_ok=True)

    database = layout["database"]
    initialize_database(database)
    RevisionStore(database).initialize()
    initialize_daily_run_schema(database)

    ledger = layout["trial_ledger"]
    if not ledger.exists():
        _write_bytes_atomic(ledger, b"")
    elif not ledger.is_file():
        raise SandboxError(f"sandbox trial ledger is not a file: {ledger}")

    registry_path = layout["holdout_registry"]
    if registry_path.exists():
        registry = _read_json(registry_path, "sandbox holdout registry")
        if registry.get("track") != TRACK:
            raise SandboxError("sandbox holdout registry is not marked track=sandbox")
    else:
        _write_json(registry_path, _initial_registry())

    manifest_path = layout["manifest"]
    payload = _manifest_payload(
        layout,
        formal_paths=resolved_formal,
        protected_roots=resolved_protected,
    )
    if manifest_path.exists():
        existing = _read_json(manifest_path, "sandbox manifest")
        if existing.get("track") != TRACK:
            raise SandboxError("sandbox manifest is not marked track=sandbox")
        if Path(str(existing.get("root"))).resolve(strict=False) != sandbox_root:
            raise SandboxError("sandbox manifest root does not match the requested root")
    else:
        _write_json(manifest_path, payload)

    return {
        "artifact_type": "kinace_v3_synthetic_sandbox_init",
        "schema_version": SCHEMA_VERSION,
        "status": "initialized",
        "track": TRACK,
        "root": str(sandbox_root),
        "sandbox_id": payload["sandbox_id"],
        "run_id_prefix": payload["run_id_prefix"],
        "database": str(database),
        "manifest_path": str(manifest_path),
        "paths": {name: str(layout[name]) for name in OWNED_SURFACES},
    }


def _json_lines(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SandboxError(f"{label} is missing: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SandboxError(f"{label} line {line_number} is invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SandboxError(f"{label} line {line_number} must be a JSON object")
        records.append(payload)
    return records


def _assert_database_schema(database: Path) -> dict[str, str]:
    if not database.is_file():
        raise SandboxError(f"sandbox database is missing: {database}")
    try:
        with sqlite3.connect(str(database)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"schema_metadata", "data_collection_schema_metadata", "daily_runs"}
            missing = sorted(required - tables)
            if missing:
                raise SandboxError(
                    "sandbox database is missing schema tables: " + ", ".join(missing)
                )
            core = connection.execute(
                "SELECT value FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()
            data_collection = connection.execute(
                "SELECT value FROM data_collection_schema_metadata WHERE key='schema_version'"
            ).fetchone()
    except sqlite3.Error as exc:
        raise SandboxError(f"cannot inspect sandbox database schema: {exc}") from exc
    return {
        "schema_version": str(core[0]) if core else "",
        "data_collection_schema_version": str(data_collection[0]) if data_collection else "",
    }


def _run_manifest_paths(layout: Mapping[str, Path]) -> list[Path]:
    root = layout["root"]
    return sorted(root.rglob("*.json"), key=lambda item: _path_key(item))


def verify_sandbox(
    *,
    root: str | os.PathLike[str],
    formal_paths: Mapping[str, str | os.PathLike[str]] | None = None,
    protected_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> dict[str, Any]:
    """Verify sandbox ownership, schema isolation, ledger identity, and run tags."""
    resolved_formal = formal_paths or configured_formal_paths()
    resolved_protected = tuple(
        PROTECTED_DATABASE_ROOTS if protected_roots is None else protected_roots
    )
    layout = resolve_sandbox_paths(
        root=root,
        formal_paths=resolved_formal,
        protected_roots=resolved_protected,
    )
    sandbox_root = layout["root"]
    if not sandbox_root.is_dir():
        raise SandboxError(f"sandbox root is missing: {sandbox_root}")
    for name in ("snapshots", "outputs", "reports", "run_ids"):
        if not layout[name].is_dir():
            raise SandboxError(f"sandbox {name} directory is missing: {layout[name]}")

    manifest = _read_json(layout["manifest"], "sandbox manifest")
    if manifest.get("track") != TRACK:
        raise SandboxError("sandbox manifest is not marked track=sandbox")
    if Path(str(manifest.get("root"))).resolve(strict=False) != sandbox_root:
        raise SandboxError("sandbox manifest root does not match the requested root")

    schema = _assert_database_schema(layout["database"])
    records = _json_lines(layout["trial_ledger"], "sandbox trial ledger")
    for record in records:
        if record.get("track") != TRACK:
            raise SandboxError("sandbox trial ledger contains a non-sandbox record")

    registry = _read_json(layout["holdout_registry"], "sandbox holdout registry")
    if registry.get("track") != TRACK:
        raise SandboxError("sandbox holdout registry is not marked track=sandbox")

    run_ids: list[str] = []
    for path in _run_manifest_paths(layout):
        payload = _read_json(path, f"sandbox run manifest {path}")
        if "run_id" not in payload and payload.get("artifact_type") not in {
            "kinace_v3_daily_run",
            "kinace_v3_search_run",
            "kinace_v3_snapshot_set_manifest",
        }:
            continue
        if payload.get("track") != TRACK:
            raise SandboxError(f"sandbox run manifest is not marked track=sandbox: {path}")
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id:
            if not run_id.startswith("sandbox-"):
                raise SandboxError(
                    f"sandbox run manifest has a non-sandbox run_id: {path}"
                )
            run_ids.append(run_id)

    identity = _sandbox_identity(sandbox_root)
    return {
        "artifact_type": "kinace_v3_synthetic_sandbox_verification",
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "track": TRACK,
        "root": str(sandbox_root),
        "sandbox_id": identity,
        "run_id_prefix": f"sandbox-{identity[:24]}",
        "manifest_path": str(layout["manifest"]),
        "database": str(layout["database"]),
        "database_schema": schema,
        "trial_count": len(records),
        "run_count": len(run_ids),
        "paths": {name: str(layout[name]) for name in OWNED_SURFACES},
    }


def write_sandbox_run_manifest(
    *,
    root: str | os.PathLike[str],
    run_id: str,
    payload: Mapping[str, Any],
    formal_paths: Mapping[str, str | os.PathLike[str]] | None = None,
    protected_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> Path:
    """Write one sandbox run manifest under the owned outputs directory."""
    resolved_formal = formal_paths or configured_formal_paths()
    resolved_protected = tuple(
        PROTECTED_DATABASE_ROOTS if protected_roots is None else protected_roots
    )
    layout = resolve_sandbox_paths(
        root=root,
        formal_paths=resolved_formal,
        protected_roots=resolved_protected,
    )
    run_id_text = str(run_id)
    if not RUN_ID_PATTERN.fullmatch(run_id_text):
        raise SandboxError(
            "sandbox run_id must be a safe single path segment starting with 'sandbox-'"
        )
    manifest = dict(payload)
    manifest.update({"track": TRACK, "run_id": run_id_text})
    target = layout["outputs"] / run_id_text / "run-manifest.json"
    if not _is_within(_resolve(target), layout["outputs"]):
        raise SandboxPathError("sandbox run manifest would escape the owned outputs root")
    return _write_json(target, manifest)


def write_sandbox_verification(
    payload: Mapping[str, Any],
    path: str | os.PathLike[str] = DEFAULT_VERIFICATION_PATH,
) -> Path:
    """Persist a sandbox verification report outside the sandbox stores."""
    return _write_json(_resolve(path), payload)


def active_track(*paths: str | os.PathLike[str] | None) -> str:
    """Return sandbox when any supplied path belongs to the active sandbox."""
    environment_root = os.environ.get(SANDBOX_ROOT_ENV)
    candidate_roots: list[Path] = []
    if environment_root:
        candidate_roots.append(_resolve(environment_root))
    for value in paths:
        if value is None:
            continue
        candidate = _resolve(value)
        for parent in (candidate, *candidate.parents):
            marker = parent / MANIFEST_FILENAME
            if marker.is_file():
                candidate_roots.append(parent)
                break
    for sandbox_root in candidate_roots:
        if any(value is not None and _is_within(_resolve(value), sandbox_root) for value in paths):
            return TRACK
    return FORMAL_TRACK


__all__ = [
    "DEFAULT_FORMAL_TRIAL_LEDGER",
    "DEFAULT_VERIFICATION_PATH",
    "FORMAL_TRACK",
    "MANIFEST_FILENAME",
    "OWNED_SURFACES",
    "SANDBOX_ROOT_ENV",
    "SCHEMA_VERSION",
    "TRACK",
    "SandboxError",
    "SandboxPathError",
    "active_track",
    "configured_formal_paths",
    "initialize_sandbox",
    "resolve_sandbox_paths",
    "sandbox_run_id",
    "verify_sandbox",
    "write_sandbox_run_manifest",
    "write_sandbox_verification",
]
