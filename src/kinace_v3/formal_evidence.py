"""Canonical formal-evidence manifests and read-only verification."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .config import PROJECT_ROOT
from .feature_snapshot import canonical_json_bytes, sha256_bytes, sha256_file
from .holdout_access import DEFAULT_FORMAL_HOLDOUT_ACCESS_REGISTRY_PATH
from .research_protocol import code_lineage, recompute_evaluation_protocol_hash


SCHEMA_VERSION = 1
MANIFEST_FILENAME = "formal-evidence-manifest.json"
VERIFICATION_FILENAME = "formal-evidence-verification.json"
DEFAULT_MANIFEST_PATH = (
    PROJECT_ROOT / "reports" / "execution_framework" / "evidence" / MANIFEST_FILENAME
)
DEFAULT_VERIFICATION_PATH = (
    PROJECT_ROOT
    / "reports"
    / "execution_framework"
    / "evidence"
    / VERIFICATION_FILENAME
)
DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "src" / "kinace_v3"
DEFAULT_SNAPSHOT_DIR = Path(r"D:\kinacesqlv3.2\snapshots_canonical")
DEFAULT_HOLDOUT_VAULT_PATH = DEFAULT_FORMAL_HOLDOUT_ACCESS_REGISTRY_PATH

MANIFEST_ARTIFACT_TYPE = "kinace_v3_formal_evidence_manifest"
VERIFICATION_ARTIFACT_TYPE = "kinace_v3_formal_evidence_verification"
ANCHOR_ARTIFACT_TYPE = "kinace_v3_formal_evidence_anchor"
ANCHOR_SUFFIX = ".anchor.json"
MANIFEST_HASH_FIELD = "manifest_sha256"
HASH_FIELDS = (
    "source_tree_sha256",
    "config_sha256",
    "database_sha256",
    "snapshot_set_sha256",
    "evaluation_protocol_sha256",
    "holdout_vault_sha256",
)
REQUIRED_MANIFEST_FIELDS = (
    "artifact_type",
    "schema_version",
    "code_commit",
    "source_root",
    "config_path",
    "database_path",
    "snapshot_dir",
    "snapshot_file_count",
    "evaluation_protocol_path",
    "holdout_vault_path",
    "holdout_access_count",
    "production_ready",
    "production_changed",
    *HASH_FIELDS,
    MANIFEST_HASH_FIELD,
)
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$", re.ASCII)
_SOURCE_EXCLUDED_PARTS = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)
_SOURCE_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


class FormalEvidenceError(ValueError):
    """Raised when formal evidence cannot be frozen or verified safely."""


def _resolve(path: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(path)))
    return Path(expanded).resolve(strict=False)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FormalEvidenceError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FormalEvidenceError(f"{field} must be a non-empty string")
    return value.strip()


def _sha256_hex(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64:
        raise FormalEvidenceError(f"{field} must be a 64-character SHA256")
    try:
        int(text, 16)
    except ValueError as exc:
        raise FormalEvidenceError(f"{field} must be hexadecimal") from exc
    return text.lower()


def _normalized_path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _paths_overlap(first: Path, second: Path) -> bool:
    first_key = _normalized_path_key(first)
    second_key = _normalized_path_key(second)
    if first_key == second_key:
        return True
    try:
        common = os.path.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in {first_key, second_key}


def _validate_no_overlap(paths: Sequence[tuple[str, Path]]) -> None:
    for index, (first_name, first_path) in enumerate(paths):
        for second_name, second_path in paths[index + 1 :]:
            if _paths_overlap(first_path, second_path):
                raise FormalEvidenceError(
                    "formal evidence artifacts overlap or contain one another: "
                    f"{first_name}={first_path}, {second_name}={second_path}"
                )


def _read_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FormalEvidenceError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalEvidenceError(f"cannot load {label}: {exc}") from exc
    return _mapping(payload, label)


def _ensure_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FormalEvidenceError(f"{label} is missing or not a file: {path}")


def _ensure_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise FormalEvidenceError(f"{label} is missing or not a directory: {path}")


def _ensure_database_quiescent(database_path: Path) -> None:
    sidecars = [
        database_path.with_name(database_path.name + suffix)
        for suffix in ("-journal", "-wal", "-shm")
    ]
    existing = [path for path in sidecars if path.exists()]
    if existing:
        raise FormalEvidenceError(
            "database WAL/journal artifacts must be absent before evidence freeze; "
            "database is writable or not quiescent: "
            + ", ".join(str(path) for path in existing)
        )


def _source_tree_sha256(source_root: Path) -> str:
    _ensure_directory(source_root, "source tree")
    entries: list[dict[str, Any]] = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        if any(part in _SOURCE_EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix.casefold() in _SOURCE_EXCLUDED_SUFFIXES:
            continue
        try:
            digest = sha256_file(path)
            size = path.stat().st_size
        except OSError as exc:
            raise FormalEvidenceError(f"cannot hash source tree file {path}: {exc}") from exc
        entries.append(
            {
                "path": relative.as_posix(),
                "size": int(size),
                "sha256": digest,
            }
        )
    if not entries:
        raise FormalEvidenceError(f"source tree contains no hashable files: {source_root}")
    entries.sort(key=lambda item: str(item["path"]).casefold())
    return sha256_bytes(canonical_json_bytes(entries, trailing_newline=False))


def source_tree_sha256(source_root: str | Path) -> str:
    """Return the canonical source-tree hash used by formal evidence."""
    return _source_tree_sha256(_resolve(source_root))


def _snapshot_set_identity(snapshot_dir: Path) -> tuple[str, int]:
    _ensure_directory(snapshot_dir, "snapshot directory")
    entries: list[tuple[str, str, int]] = []
    for path in snapshot_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(snapshot_dir).as_posix()
        try:
            entries.append(
                (relative, sha256_file(path), int(path.stat().st_size))
            )
        except OSError as exc:
            raise FormalEvidenceError(f"cannot hash snapshot artifact {path}: {exc}") from exc
    if not entries:
        raise FormalEvidenceError(f"snapshot set contains no files: {snapshot_dir}")
    entries.sort(key=lambda item: (item[0].casefold(), item[0]))
    lines = [f"{digest}  {size}  {relative}" for relative, digest, size in entries]
    digest = sha256_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return digest, len(entries)


def snapshot_set_sha256(snapshot_dir: str | Path) -> str:
    """Return the aggregate SHA256 over a deterministic snapshot file set."""
    return _snapshot_set_identity(_resolve(snapshot_dir))[0]


def _evaluation_protocol_identity(protocol_path: Path) -> tuple[str, str]:
    _ensure_file(protocol_path, "evaluation protocol")
    raw_sha256 = sha256_file(protocol_path)
    payload = _read_json_object(protocol_path, "evaluation protocol")
    declared = payload.get("evaluation_protocol_hash")
    if declared is None:
        return raw_sha256, raw_sha256
    declared_hash = _sha256_hex(declared, "evaluation_protocol_hash")
    try:
        recomputed = recompute_evaluation_protocol_hash(payload)
    except Exception as exc:
        raise FormalEvidenceError(
            f"evaluation protocol hash cannot be recomputed: {exc}"
        ) from exc
    if declared_hash != recomputed.lower():
        raise FormalEvidenceError(
            "evaluation protocol hash changed or was tampered with"
        )
    return raw_sha256, declared_hash


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    identity = {
        str(key): value
        for key, value in manifest.items()
        if key != MANIFEST_HASH_FIELD
    }
    return sha256_bytes(canonical_json_bytes(identity, trailing_newline=False))


def manifest_anchor_path(path: str | os.PathLike[str]) -> Path:
    """Return the external anchor path for one formal evidence manifest."""
    target = _resolve(path)
    return target.with_name(target.name + ANCHOR_SUFFIX)


def _manifest_anchor_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        MANIFEST_HASH_FIELD,
        "code_commit",
        "source_tree_sha256",
        "config_sha256",
        "database_sha256",
        "snapshot_set_sha256",
        "evaluation_protocol_sha256",
        "evaluation_protocol_hash",
        "holdout_vault_sha256",
        "holdout_access_count",
        "production_ready",
        "production_changed",
    )
    return {
        "artifact_type": ANCHOR_ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        **{field: manifest[field] for field in fields},
    }


def _verify_manifest_anchor(manifest_path: Path, manifest: Mapping[str, Any]) -> None:
    anchor_path = manifest_anchor_path(manifest_path)
    _ensure_file(anchor_path, "formal evidence anchor")
    anchor = _read_json_object(anchor_path, "formal evidence anchor")
    if anchor.get("artifact_type") != ANCHOR_ARTIFACT_TYPE:
        raise FormalEvidenceError("formal evidence anchor has the wrong artifact_type")
    if anchor.get("schema_version") != SCHEMA_VERSION:
        raise FormalEvidenceError("formal evidence anchor has an unsupported schema_version")
    expected = _manifest_anchor_payload(manifest)
    if dict(anchor) != expected:
        raise FormalEvidenceError("formal evidence manifest changed relative to its external anchor")
    _assert_read_only(anchor_path, "formal evidence anchor")


def _validate_manifest_integrity(manifest: Mapping[str, Any]) -> None:
    if manifest.get("artifact_type") != MANIFEST_ARTIFACT_TYPE:
        raise FormalEvidenceError("formal evidence manifest has the wrong artifact_type")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise FormalEvidenceError("formal evidence manifest has an unsupported schema_version")
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise FormalEvidenceError(
            "formal evidence manifest is missing required fields: " + ", ".join(missing)
        )
    code_commit = _required_text(manifest.get("code_commit"), "code_commit")
    if not _COMMIT_PATTERN.fullmatch(code_commit):
        raise FormalEvidenceError("code_commit must be a hexadecimal Git commit identifier")
    for field in HASH_FIELDS:
        _sha256_hex(manifest.get(field), field)
    access_count = manifest.get("holdout_access_count")
    if (
        isinstance(access_count, bool)
        or not isinstance(access_count, int)
        or access_count < 0
    ):
        raise FormalEvidenceError("holdout_access_count must be a non-negative integer")
    snapshot_count = manifest.get("snapshot_file_count")
    if (
        isinstance(snapshot_count, bool)
        or not isinstance(snapshot_count, int)
        or snapshot_count <= 0
    ):
        raise FormalEvidenceError("snapshot_file_count must be a positive integer")
    for field in ("production_ready", "production_changed"):
        if not isinstance(manifest.get(field), bool):
            raise FormalEvidenceError(f"{field} must be boolean")
    expected = _sha256_hex(manifest.get(MANIFEST_HASH_FIELD), MANIFEST_HASH_FIELD)
    actual = _manifest_digest(manifest)
    if expected != actual:
        raise FormalEvidenceError(
            "formal evidence manifest hash changed or was tampered with"
        )


def _artifact_paths(manifest: Mapping[str, Any]) -> tuple[tuple[str, Path], ...]:
    return (
        ("source_root", _resolve(_required_text(manifest.get("source_root"), "source_root"))),
        ("config_path", _resolve(_required_text(manifest.get("config_path"), "config_path"))),
        (
            "database_path",
            _resolve(_required_text(manifest.get("database_path"), "database_path")),
        ),
        (
            "snapshot_dir",
            _resolve(_required_text(manifest.get("snapshot_dir"), "snapshot_dir")),
        ),
        (
            "evaluation_protocol_path",
            _resolve(
                _required_text(
                    manifest.get("evaluation_protocol_path"),
                    "evaluation_protocol_path",
                )
            ),
        ),
        (
            "holdout_vault_path",
            _resolve(
                _required_text(
                    manifest.get("holdout_vault_path"), "holdout_vault_path"
                )
            ),
        ),
    )


def _assert_artifacts_exist(paths: Sequence[tuple[str, Path]]) -> None:
    for label, path in paths:
        if label in {"source_root", "snapshot_dir"}:
            _ensure_directory(path, label)
        else:
            _ensure_file(path, label)


def _assert_read_only(path: Path, label: str) -> None:
    if path.is_symlink():
        raise FormalEvidenceError(f"{label} must not be a symbolic link: {path}")
    try:
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    except FileNotFoundError as exc:
        raise FormalEvidenceError(f"{label} is missing: {path}") from exc
    if mode & _WRITE_BITS:
        raise FormalEvidenceError(
            f"{label} is writable; formal evidence must be read-only: {path}"
        )


def _assert_tree_read_only(root: Path, label: str) -> None:
    paths = [root]
    paths.extend(sorted(root.rglob("*"), key=lambda item: str(item).casefold()))
    for path in paths:
        _assert_read_only(path, f"{label} artifact")


def build_formal_evidence_manifest(
    *,
    source_root: str | Path,
    code_commit: str | None = None,
    config_path: str | Path,
    database_path: str | Path,
    snapshot_dir: str | Path,
    evaluation_protocol_path: str | Path,
    holdout_vault_path: str | Path,
    holdout_access_count: int,
    production_ready: bool,
    production_changed: bool,
) -> dict[str, Any]:
    """Build a deterministic manifest binding the formal evidence surfaces."""
    resolved_source = _resolve(source_root)
    resolved_config = _resolve(config_path)
    resolved_database = _resolve(database_path)
    resolved_snapshots = _resolve(snapshot_dir)
    resolved_protocol = _resolve(evaluation_protocol_path)
    resolved_holdout = _resolve(holdout_vault_path)
    paths = (
        ("source_root", resolved_source),
        ("config_path", resolved_config),
        ("database_path", resolved_database),
        ("snapshot_dir", resolved_snapshots),
        ("evaluation_protocol_path", resolved_protocol),
        ("holdout_vault_path", resolved_holdout),
    )
    _validate_no_overlap(paths)
    for label, path in paths:
        if label in {"source_root", "snapshot_dir"}:
            _ensure_directory(path, label)
        else:
            _ensure_file(path, label)
    _ensure_database_quiescent(resolved_database)

    if code_commit is None:
        code_commit = str(code_lineage(__version__).get("code_commit_sha") or "")
    commit = _required_text(code_commit, "code_commit")
    if not _COMMIT_PATTERN.fullmatch(commit):
        raise FormalEvidenceError("code_commit must be a hexadecimal Git commit identifier")
    if (
        isinstance(holdout_access_count, bool)
        or not isinstance(holdout_access_count, int)
        or holdout_access_count < 0
    ):
        raise FormalEvidenceError("holdout_access_count must be a non-negative integer")
    if not isinstance(production_ready, bool):
        raise FormalEvidenceError("production_ready must be boolean")
    if not isinstance(production_changed, bool):
        raise FormalEvidenceError("production_changed must be boolean")

    snapshot_hash, snapshot_count = _snapshot_set_identity(resolved_snapshots)
    protocol_sha256, protocol_hash = _evaluation_protocol_identity(resolved_protocol)
    manifest: dict[str, Any] = {
        "artifact_type": MANIFEST_ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "code_version": __version__,
        "code_commit": commit.lower(),
        "source_root": str(resolved_source),
        "source_tree_sha256": _source_tree_sha256(resolved_source),
        "config_path": str(resolved_config),
        "config_sha256": sha256_file(resolved_config),
        "database_path": str(resolved_database),
        "database_sha256": sha256_file(resolved_database),
        "snapshot_dir": str(resolved_snapshots),
        "snapshot_file_count": snapshot_count,
        "snapshot_set_sha256": snapshot_hash,
        "snapshot_set_hash_definition": (
            "sha256 of newline-delimited sorted relative-path entries formatted as "
            "per-file-sha256, two spaces, size, two spaces, relative-path"
        ),
        "evaluation_protocol_path": str(resolved_protocol),
        "evaluation_protocol_sha256": protocol_sha256,
        "evaluation_protocol_hash": protocol_hash,
        "holdout_vault_path": str(resolved_holdout),
        "holdout_vault_sha256": sha256_file(resolved_holdout),
        "holdout_access_count": holdout_access_count,
        "production_ready": production_ready,
        "production_changed": production_changed,
    }
    manifest["holdout_vault_id"] = manifest["holdout_vault_sha256"]
    manifest[MANIFEST_HASH_FIELD] = _manifest_digest(manifest)
    return manifest


def write_formal_evidence_manifest(
    manifest: Mapping[str, Any],
    path: str | Path,
) -> Path:
    """Write one canonical manifest without overwriting existing evidence."""
    payload = dict(_mapping(manifest, "formal evidence manifest"))
    _validate_manifest_integrity(payload)
    target = _resolve(path)
    if target.exists():
        raise FormalEvidenceError(
            f"formal evidence manifest already exists and will not be overwritten: {target}"
        )
    anchor_target = manifest_anchor_path(target)
    if anchor_target.exists():
        raise FormalEvidenceError(
            f"formal evidence anchor already exists and will not be overwritten: {anchor_target}"
        )
    _validate_no_overlap([*_artifact_paths(payload), ("manifest_path", target)])
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        anchor_temporary = anchor_target.with_suffix(anchor_target.suffix + ".tmp")
        if temporary.exists():
            raise FormalEvidenceError(
                f"temporary formal evidence manifest already exists: {temporary}"
            )
        if anchor_temporary.exists():
            raise FormalEvidenceError(
                f"temporary formal evidence anchor already exists: {anchor_temporary}"
            )
        temporary.write_bytes(canonical_json_bytes(payload))
        temporary.replace(target)
        anchor_temporary.write_bytes(
            canonical_json_bytes(_manifest_anchor_payload(payload))
        )
        anchor_temporary.replace(anchor_target)
    except FormalEvidenceError:
        raise
    except OSError as exc:
        raise FormalEvidenceError(f"cannot write formal evidence manifest: {exc}") from exc
    return target


def verify_formal_evidence_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Verify a manifest and all bound artifacts without mutating any evidence."""
    target = _resolve(manifest_path)
    _ensure_file(target, "formal evidence manifest")
    manifest = dict(_read_json_object(target, "formal evidence manifest"))
    _validate_manifest_integrity(manifest)
    _verify_manifest_anchor(target, manifest)
    paths = _artifact_paths(manifest)
    path_map = dict(paths)
    _validate_no_overlap([*paths, ("manifest_path", target)])
    _assert_artifacts_exist(paths)
    _ensure_database_quiescent(path_map["database_path"])

    snapshot_set_hash, snapshot_file_count = _snapshot_set_identity(
        path_map["snapshot_dir"]
    )
    actual: dict[str, str] = {
        "source_tree_sha256": _source_tree_sha256(path_map["source_root"]),
        "config_sha256": sha256_file(path_map["config_path"]),
        "database_sha256": sha256_file(path_map["database_path"]),
        "snapshot_set_sha256": snapshot_set_hash,
        "holdout_vault_sha256": sha256_file(path_map["holdout_vault_path"]),
    }
    if int(manifest["snapshot_file_count"]) != snapshot_file_count:
        raise FormalEvidenceError("snapshot_file_count changed or was tampered with")
    protocol_sha256, protocol_hash = _evaluation_protocol_identity(
        path_map["evaluation_protocol_path"]
    )
    actual["evaluation_protocol_sha256"] = protocol_sha256
    for field, digest in actual.items():
        expected = _sha256_hex(manifest.get(field), field)
        if expected != digest:
            raise FormalEvidenceError(
                f"{field} changed or was tampered with: expected {expected}, got {digest}"
            )
    expected_protocol_hash = _sha256_hex(
        manifest.get("evaluation_protocol_hash", protocol_sha256),
        "evaluation_protocol_hash",
    )
    if expected_protocol_hash != _sha256_hex(
        protocol_hash, "evaluation_protocol_hash"
    ):
        raise FormalEvidenceError(
            "evaluation_protocol_hash changed or was tampered with"
        )
    if _sha256_hex(manifest.get("holdout_vault_id"), "holdout_vault_id") != actual[
        "holdout_vault_sha256"
    ]:
        raise FormalEvidenceError("holdout_vault_id changed or was tampered with")

    _assert_read_only(target, "formal evidence manifest")
    for label, path in paths:
        if label in {"source_root", "snapshot_dir"}:
            _assert_tree_read_only(path, label)
        else:
            _assert_read_only(path, label)

    return {
        "artifact_type": VERIFICATION_ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "read_only": True,
        "manifest_path": str(target),
        "manifest_sha256": str(manifest[MANIFEST_HASH_FIELD]),
        "code_commit": str(manifest["code_commit"]),
        "source_tree_sha256": actual["source_tree_sha256"],
        "config_sha256": actual["config_sha256"],
        "database_sha256": actual["database_sha256"],
        "snapshot_set_sha256": actual["snapshot_set_sha256"],
        "evaluation_protocol_sha256": actual["evaluation_protocol_sha256"],
        "evaluation_protocol_hash": protocol_hash,
        "holdout_vault_sha256": actual["holdout_vault_sha256"],
        "holdout_access_count": int(manifest["holdout_access_count"]),
        "production_ready": bool(manifest["production_ready"]),
        "production_changed": bool(manifest["production_changed"]),
    }


__all__ = [
    "DEFAULT_HOLDOUT_VAULT_PATH",
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_SNAPSHOT_DIR",
    "DEFAULT_SOURCE_ROOT",
    "DEFAULT_VERIFICATION_PATH",
    "FormalEvidenceError",
    "HASH_FIELDS",
    "MANIFEST_ARTIFACT_TYPE",
    "MANIFEST_HASH_FIELD",
    "REQUIRED_MANIFEST_FIELDS",
    "SCHEMA_VERSION",
    "VERIFICATION_ARTIFACT_TYPE",
    "build_formal_evidence_manifest",
    "snapshot_set_sha256",
    "source_tree_sha256",
    "verify_formal_evidence_manifest",
    "write_formal_evidence_manifest",
]
