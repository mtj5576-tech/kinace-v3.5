"""Append-only one-shot authorization for fixed-holdout reads."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import PROJECT_ROOT
from .feature_snapshot import canonical_json_bytes, sha256_bytes


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "kinace_v3_holdout_access_registry"
FORMAL_TRACK = "formal"
SANDBOX_TRACK = "sandbox"
TRACKS = (FORMAL_TRACK, SANDBOX_TRACK)
FORMAL_REGISTRY_FILENAME = "holdout_access_registry.json"
SANDBOX_REGISTRY_FILENAME = "holdout-access-registry.json"
DEFAULT_FORMAL_HOLDOUT_ACCESS_REGISTRY_PATH = (
    PROJECT_ROOT / "reports" / FORMAL_REGISTRY_FILENAME
)


class HoldoutAccessError(ValueError):
    """Raised when a holdout access registry cannot be used safely."""


class HoldoutNotEligibleError(HoldoutAccessError):
    """Raised when a candidate has not passed every non-holdout gate."""

    reason_code = "holdout_not_eligible"


class HoldoutConsumedError(HoldoutAccessError):
    """Raised when the one authorized holdout evaluation is reused."""

    reason_code = "holdout_consumed"


def _resolve(path: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(path)))
    return Path(expanded).resolve(strict=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_track(track: str) -> str:
    value = str(track).strip().casefold()
    if value not in TRACKS:
        raise HoldoutAccessError("holdout track must be formal or sandbox")
    return value


def default_holdout_access_registry_path(track: str = FORMAL_TRACK) -> Path:
    """Return the default registry path for one isolated access track."""
    normalized = _normalize_track(track)
    if normalized == FORMAL_TRACK:
        return _resolve(DEFAULT_FORMAL_HOLDOUT_ACCESS_REGISTRY_PATH)
    root_value = os.environ.get("KINACE_SANDBOX_ROOT")
    if not root_value:
        raise HoldoutAccessError(
            "sandbox holdout access requires KINACE_SANDBOX_ROOT or an explicit registry path"
        )
    return _resolve(root_value) / SANDBOX_REGISTRY_FILENAME


def resolve_holdout_access_registry_path(
    path: str | os.PathLike[str] | None = None,
    *,
    track: str = FORMAL_TRACK,
) -> Path:
    normalized = _normalize_track(track)
    return (
        default_holdout_access_registry_path(normalized)
        if path is None
        else _resolve(path)
    )


def _registry_id(path: Path, track: str) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {"path": os.path.normcase(os.path.normpath(str(path))), "track": track},
            trailing_newline=False,
        )
    )


def _initial_registry(path: Path, track: str) -> dict[str, Any]:
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "track": track,
        "registry_id": _registry_id(path, track),
        "access_count": 0,
        "read_count": 0,
        "pre_eligible_count": 0,
        "pre_eligible_access_count": 0,
        "extension_count": 0,
        "entries": [],
        "extension_bindings": [],
        "updated_at_utc": None,
    }


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HoldoutAccessError(f"{field} must be an object")
    return value


def _read_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutAccessError(f"cannot load holdout access registry: {exc}") from exc
    return dict(_mapping(payload, "holdout access registry"))


def _write_registry(payload: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    temporary.replace(path)
    return path


def _validate_registry_payload(
    payload: Mapping[str, Any],
    *,
    path: Path,
    track: str,
) -> dict[str, Any]:
    result = dict(payload)
    if result.get("artifact_type") != ARTIFACT_TYPE:
        raise HoldoutAccessError("holdout access registry has the wrong artifact_type")
    if int(result.get("schema_version") or 0) != SCHEMA_VERSION:
        raise HoldoutAccessError("holdout access registry has an unsupported schema_version")
    registry_track = _normalize_track(result.get("track"))
    if registry_track != track:
        raise HoldoutAccessError(
            f"holdout access registry track mismatch: expected {track}, got {registry_track}"
        )
    expected_registry_id = _registry_id(path, track)
    actual_registry_id = str(result.get("registry_id") or "")
    if actual_registry_id and actual_registry_id != expected_registry_id:
        raise HoldoutAccessError("holdout access registry ID does not match its path and track")
    entries = result.get("entries")
    if not isinstance(entries, list):
        raise HoldoutAccessError("holdout access registry entries must be a list")
    normalized_entries: list[dict[str, Any]] = []
    access_count = 0
    seen_ids: set[str] = set()
    seen_protocol_windows: set[tuple[str, str]] = set()
    for index, value in enumerate(entries):
        entry = dict(_mapping(value, f"holdout access entry {index}"))
        entry_track = _normalize_track(entry.get("track"))
        if entry_track != track:
            raise HoldoutAccessError("holdout access entry track does not match its registry")
        access_id = str(entry.get("holdout_access_id") or "")
        if not access_id or access_id in seen_ids:
            raise HoldoutAccessError("holdout access IDs must be non-empty and unique")
        seen_ids.add(access_id)
        read_count = entry.get("read_count")
        if isinstance(read_count, bool) or not isinstance(read_count, int) or read_count < 0:
            raise HoldoutAccessError("holdout access read_count must be a non-negative integer")
        if read_count != 1:
            raise HoldoutAccessError("each holdout access entry must have read_count=1")
        if entry.get("eligible") is not True:
            raise HoldoutAccessError("holdout access entries must be marked eligible")
        window = _mapping(entry.get("window"), "holdout access window")
        protocol = str(entry.get("protocol") or entry.get("protocol_hash") or "")
        window_key = _window_key(window)
        key = (protocol, window_key)
        if not protocol or key in seen_protocol_windows:
            raise HoldoutAccessError("holdout protocol/window may be consumed only once")
        seen_protocol_windows.add(key)
        normalized_entries.append(entry)
        access_count += read_count
    declared_count = result.get("access_count")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != access_count
    ):
        raise HoldoutAccessError("holdout access_count does not match registry entries")
    declared_read_count = result.get("read_count", access_count)
    if (
        isinstance(declared_read_count, bool)
        or not isinstance(declared_read_count, int)
        or declared_read_count != access_count
    ):
        raise HoldoutAccessError("holdout read_count does not match registry entries")
    pre_eligible = result.get("pre_eligible_count", 0)
    pre_eligible_access = result.get("pre_eligible_access_count", 0)
    if pre_eligible not in {0, None} or pre_eligible_access not in {0, None}:
        raise HoldoutAccessError("pre-eligible holdout access count must remain zero")
    bindings = result.get("extension_bindings", [])
    if not isinstance(bindings, list):
        raise HoldoutAccessError("holdout extension bindings must be a list")
    normalized_bindings = [
        dict(_mapping(value, f"holdout extension binding {index}"))
        for index, value in enumerate(bindings)
    ]
    result.update(
        {
            "artifact_type": ARTIFACT_TYPE,
            "schema_version": SCHEMA_VERSION,
            "track": track,
            "registry_id": expected_registry_id,
            "access_count": access_count,
            "read_count": access_count,
            "pre_eligible_count": 0,
            "pre_eligible_access_count": 0,
            "extension_count": len(normalized_bindings),
            "entries": normalized_entries,
            "extension_bindings": normalized_bindings,
        }
    )
    return result


def load_holdout_access_registry(
    path: str | os.PathLike[str] | None = None,
    *,
    track: str = FORMAL_TRACK,
) -> dict[str, Any]:
    """Load and validate one append-only access registry without mutation."""
    normalized = _normalize_track(track)
    target = resolve_holdout_access_registry_path(path, track=normalized)
    if not target.is_file():
        return _initial_registry(target, normalized)
    return _validate_registry_payload(_read_registry(target), path=target, track=normalized)


def holdout_access_count(
    path: str | os.PathLike[str] | None = None,
    *,
    track: str = FORMAL_TRACK,
) -> int:
    return int(load_holdout_access_registry(path, track=track)["access_count"])


def _candidate_value(candidate: Any, candidate_signature: str | None = None) -> str:
    if candidate_signature:
        return str(candidate_signature)
    if isinstance(candidate, Mapping):
        for key in ("candidate_signature", "signature", "id", "candidate_id"):
            value = candidate.get(key)
            if value not in {None, ""}:
                return str(value)
        raise HoldoutAccessError("candidate must contain a candidate signature")
    value = str(candidate or "").strip()
    if not value:
        raise HoldoutAccessError("candidate must be a non-empty string")
    return value


def _protocol_value(protocol: Any, protocol_hash: str | None = None) -> str:
    if protocol_hash:
        return str(protocol_hash)
    if isinstance(protocol, Mapping):
        for key in ("evaluation_protocol_hash", "protocol_hash", "protocol_id", "id"):
            value = protocol.get(key)
            if value not in {None, ""}:
                return str(value)
        raise HoldoutAccessError("protocol must contain a protocol hash")
    value = str(protocol or "").strip()
    if not value:
        raise HoldoutAccessError("protocol must be a non-empty string")
    return value


def _window_payload(window: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(_mapping(window, "holdout window"))
    start = str(source.get("start") or source.get("start_date") or "").strip()
    end = str(source.get("end") or source.get("end_date") or "").strip()
    if not start or not end:
        raise HoldoutAccessError("holdout window must define start and end")
    sessions = source.get("sessions")
    if sessions is not None:
        if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions <= 0:
            raise HoldoutAccessError("holdout window sessions must be a positive integer")
    return {"start": start, "end": end, "sessions": sessions}


def _window_key(window: Mapping[str, Any]) -> str:
    normalized = _window_payload(window)
    return sha256_bytes(
        canonical_json_bytes(normalized, trailing_newline=False)
    )


def _windows_overlap(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    first_window = _window_payload(first)
    second_window = _window_payload(second)
    return (
        first_window["start"] <= second_window["end"]
        and second_window["start"] <= first_window["end"]
    )


def _access_id(
    *,
    registry_id: str,
    track: str,
    candidate: str,
    protocol: str,
    window_key: str,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "registry_id": registry_id,
                "track": track,
                "candidate": candidate,
                "protocol": protocol,
                "window": window_key,
            },
            trailing_newline=False,
        )
    )


def open_holdout_access(
    candidate: Any,
    protocol: Any,
    window: Mapping[str, Any],
    *,
    eligible: bool | None = None,
    non_holdout_gates: Mapping[str, Any] | None = None,
    registry_path: str | os.PathLike[str] | None = None,
    track: str = FORMAL_TRACK,
    candidate_signature: str | None = None,
    protocol_hash: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Consume the single authorized read for one protocol and holdout window."""
    normalized_track = _normalize_track(track)
    if eligible is None:
        gates = non_holdout_gates or {}
        eligible = bool(gates) and all(bool(value) for value in gates.values())
    if eligible is not True:
        raise HoldoutNotEligibleError(
            "holdout is locked until every non-holdout gate passes"
        )

    candidate_value = _candidate_value(candidate, candidate_signature)
    protocol_value = _protocol_value(protocol, protocol_hash)
    window_payload = _window_payload(window)
    path = resolve_holdout_access_registry_path(
        registry_path,
        track=normalized_track,
    )
    registry = load_holdout_access_registry(path, track=normalized_track)
    window_key = _window_key(window_payload)
    for entry in registry["entries"]:
        if (
            str(entry.get("protocol") or "") == protocol_value
            and _windows_overlap(
                _mapping(entry.get("window"), "entry.window"),
                window_payload,
            )
        ):
            raise HoldoutConsumedError(
                "holdout_consumed: this protocol/window has already been evaluated"
            )

    now = timestamp or _utc_now()
    access_id = _access_id(
        registry_id=registry["registry_id"],
        track=normalized_track,
        candidate=candidate_value,
        protocol=protocol_value,
        window_key=window_key,
    )
    entry = {
        "holdout_access_id": access_id,
        "candidate": candidate_value,
        "candidate_signature": candidate_value,
        "protocol": protocol_value,
        "protocol_hash": protocol_value,
        "track": normalized_track,
        "window": window_payload,
        "window_id": window_key,
        "eligible": True,
        "read_count": 1,
        "status": "consumed",
        "authorized_at_utc": now,
        "opened_at_utc": now,
        "read_at_utc": now,
        "created_at_utc": now,
        "timestamps": {
            "authorized_at_utc": now,
            "opened_at_utc": now,
            "read_at_utc": now,
        },
    }
    registry["entries"].append(entry)
    registry["access_count"] = int(registry["access_count"]) + 1
    registry["read_count"] = int(registry["access_count"])
    registry["updated_at_utc"] = now
    _write_registry(registry, path)
    return {**entry, "registry_path": str(path), "registry_id": registry["registry_id"]}


def authorize_holdout_access(
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility alias for callers using authorization terminology."""
    return open_holdout_access(*args, **kwargs)


def get_holdout_access(
    holdout_access_id: str,
    *,
    registry_path: str | os.PathLike[str] | None = None,
    track: str = FORMAL_TRACK,
) -> dict[str, Any] | None:
    registry = load_holdout_access_registry(registry_path, track=track)
    resolved = resolve_holdout_access_registry_path(registry_path, track=track)
    return next(
        (
            {
                **entry,
                "registry_path": str(resolved),
                "registry_id": registry["registry_id"],
            }
            for entry in registry["entries"]
            if str(entry.get("holdout_access_id") or "") == str(holdout_access_id)
        ),
        None,
    )


def bind_holdout_extension_access(
    *,
    holdout_access_id: str,
    candidate: Any,
    protocol: Any,
    extension_window: Mapping[str, Any],
    freeze_id: str,
    extension_policy_id: str,
    registry_path: str | os.PathLike[str] | None = None,
    track: str = FORMAL_TRACK,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Bind a pre-registered extension to its existing authorization."""
    normalized_track = _normalize_track(track)
    path = resolve_holdout_access_registry_path(registry_path, track=normalized_track)
    registry = load_holdout_access_registry(path, track=normalized_track)
    access = next(
        (
            entry
            for entry in registry["entries"]
            if str(entry.get("holdout_access_id") or "") == str(holdout_access_id)
        ),
        None,
    )
    if access is None:
        raise HoldoutConsumedError(
            "holdout_consumed: extension authorization does not exist"
        )
    candidate_value = _candidate_value(candidate)
    protocol_value = _protocol_value(protocol)
    if (
        str(access.get("candidate") or "") != candidate_value
        or str(access.get("protocol") or "") != protocol_value
        or str(access.get("track") or "") != normalized_track
        or int(access.get("read_count") or 0) != 1
    ):
        raise HoldoutConsumedError(
            "holdout_consumed: extension does not match the original authorization"
        )
    window = _window_payload(extension_window)
    initial_window = _window_payload(_mapping(access.get("window"), "access.window"))
    if (
        window["start"] != initial_window["start"]
        or window["end"] < initial_window["end"]
        or (
            window["sessions"] is not None
            and initial_window["sessions"] is not None
            and window["sessions"] < initial_window["sessions"]
        )
    ):
        raise HoldoutConsumedError(
            "holdout_consumed: extension window is not an extension of the original holdout"
        )
    window_key = _window_key(window)
    for binding in registry["extension_bindings"]:
        if (
            str(binding.get("holdout_access_id") or "") == str(holdout_access_id)
            and str(binding.get("window_id") or "") == window_key
        ):
            raise HoldoutConsumedError(
                "holdout_consumed: this holdout extension was already evaluated"
            )
    now = timestamp or _utc_now()
    binding = {
        "holdout_access_id": str(holdout_access_id),
        "candidate": candidate_value,
        "protocol": protocol_value,
        "track": normalized_track,
        "freeze_id": str(freeze_id),
        "extension_policy_id": str(extension_policy_id),
        "window": window,
        "window_id": window_key,
        "read_count_delta": 0,
        "reused_at_utc": now,
        "created_at_utc": now,
    }
    registry["extension_bindings"].append(binding)
    registry["extension_count"] = len(registry["extension_bindings"])
    registry["updated_at_utc"] = now
    _write_registry(registry, path)
    return {
        **binding,
        "registry_path": str(path),
        "registry_id": registry["registry_id"],
        "read_count": int(access["read_count"]),
    }


def assert_registry_separation(
    formal_registry_path: str | os.PathLike[str],
    sandbox_registry_path: str | os.PathLike[str],
) -> None:
    """Reject shared paths, IDs, or records between formal and sandbox tracks."""
    formal_path = _resolve(formal_registry_path)
    sandbox_path = _resolve(sandbox_registry_path)
    formal_key = os.path.normcase(os.path.normpath(str(formal_path)))
    sandbox_key = os.path.normcase(os.path.normpath(str(sandbox_path)))
    try:
        common = os.path.commonpath((formal_key, sandbox_key))
    except ValueError:
        common = ""
    if common in {formal_key, sandbox_key}:
        raise HoldoutAccessError("formal and sandbox holdout registries must be separate")
    formal = load_holdout_access_registry(formal_path, track=FORMAL_TRACK)
    sandbox = load_holdout_access_registry(sandbox_path, track=SANDBOX_TRACK)
    if formal["registry_id"] == sandbox["registry_id"]:
        raise HoldoutAccessError("formal and sandbox holdout registry IDs must differ")
    formal_ids = {str(item.get("holdout_access_id") or "") for item in formal["entries"]}
    sandbox_ids = {str(item.get("holdout_access_id") or "") for item in sandbox["entries"]}
    if formal_ids & sandbox_ids:
        raise HoldoutAccessError("formal and sandbox holdout access IDs must differ")
    for formal_entry in formal["entries"]:
        for sandbox_entry in sandbox["entries"]:
            if formal_entry == sandbox_entry:
                raise HoldoutAccessError("formal and sandbox holdout records must differ")


__all__ = [
    "ARTIFACT_TYPE",
    "DEFAULT_FORMAL_HOLDOUT_ACCESS_REGISTRY_PATH",
    "FORMAL_REGISTRY_FILENAME",
    "FORMAL_TRACK",
    "HoldoutAccessError",
    "HoldoutConsumedError",
    "HoldoutNotEligibleError",
    "SANDBOX_REGISTRY_FILENAME",
    "SANDBOX_TRACK",
    "SCHEMA_VERSION",
    "TRACKS",
    "assert_registry_separation",
    "authorize_holdout_access",
    "bind_holdout_extension_access",
    "default_holdout_access_registry_path",
    "get_holdout_access",
    "holdout_access_count",
    "load_holdout_access_registry",
    "open_holdout_access",
    "resolve_holdout_access_registry_path",
]
