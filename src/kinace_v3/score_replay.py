from __future__ import annotations

import hashlib
import json
import math
import statistics
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .feature_snapshot import (
    FEATURE_NAMES,
    MODEL_PENALTY_NAMES,
    PENALTY_INPUT_NAMES,
    FeatureSnapshotError,
    canonical_json_bytes,
    compute_z_score_inputs,
    load_feature_snapshot_artifact,
    sha256_bytes,
    sha256_file,
    validate_model_config,
)
from .research_feature_transform import apply_raw_input_overrides


SCHEMA_VERSION = 1


class ScoreReplayError(ValueError):
    """Raised when a candidate score replay cannot be reproduced."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScoreReplayError(f"{field} must be an object")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreReplayError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ScoreReplayError(f"{field} must be a finite number")
    return number


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, field)


def validate_candidate_config(candidate_config: Mapping[str, Any]) -> dict[str, Any]:
    """Return an exact, validated candidate configuration."""
    try:
        result = validate_model_config(candidate_config)
    except FeatureSnapshotError as exc:
        raise ScoreReplayError(str(exc)) from exc
    result = deepcopy(result)
    result["weights"] = {
        name: _finite_number(result["weights"][name], f"weights.{name}")
        for name in FEATURE_NAMES
    }
    result["penalties"] = {
        name: _finite_number(result["penalties"][name], f"penalties.{name}")
        for name in MODEL_PENALTY_NAMES
    }
    return result


def candidate_id_for(candidate_config: Mapping[str, Any]) -> str:
    candidate = validate_candidate_config(candidate_config)
    payload = canonical_json_bytes(candidate, trailing_newline=False)
    return hashlib.sha256(payload).hexdigest()


def _stable_logistic(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def normalize_score_policy(
    config: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if config is None:
        return None
    source = _mapping(config, "score_policy")
    unknown = sorted(set(source) - {"persistence_sessions", "persistence_weight"})
    if unknown:
        raise ScoreReplayError("unknown score_policy keys: " + ", ".join(unknown))
    sessions = source.get("persistence_sessions")
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions <= 0:
        raise ScoreReplayError("score_policy.persistence_sessions must be positive")
    weight = _finite_number(
        source.get("persistence_weight"),
        "score_policy.persistence_weight",
    )
    if not 0 <= weight <= 1:
        raise ScoreReplayError("score_policy.persistence_weight must be in [0, 1]")
    return {
        "persistence_sessions": sessions,
        "persistence_weight": weight,
    }


def apply_score_persistence(
    scores: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    settings = normalize_score_policy(policy)
    if settings is None:
        return [deepcopy(dict(score)) for score in scores]
    sessions = int(settings["persistence_sessions"])
    weight = float(settings["persistence_weight"])
    history: dict[str, list[float]] = {}
    result: list[dict[str, Any]] = []
    for score in sorted(
        scores,
        key=lambda item: (str(item.get("decision_date") or ""), str(item.get("code") or "")),
    ):
        row = deepcopy(dict(score))
        code = str(row.get("code") or "")
        adjusted = _finite_number(row.get("adjusted_score"), "score.adjusted_score")
        previous = history.get(code, [])[-sessions:]
        smoothed = adjusted
        if len(previous) >= sessions:
            smoothed = (1.0 - weight) * adjusted + weight * statistics.fmean(previous)
        row["adjusted_score"] = smoothed
        row["score"] = round(_stable_logistic(smoothed), 6)
        row["persistence"] = {
            "sessions": len(previous),
            "weight": weight,
            "original_adjusted_score": adjusted,
            "applied": len(previous) >= sessions,
        }
        history.setdefault(code, []).append(adjusted)
        result.append(row)
    return result


def _snapshot_input(snapshot: Mapping[str, Any] | str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(snapshot, (str, Path)):
        payload = load_feature_snapshot_artifact(snapshot)
        reference = {
            "snapshot_id": sha256_file(snapshot),
            "artifact_sha256": sha256_file(snapshot),
            "artifact_path": str(Path(snapshot).resolve(strict=False)),
        }
        return payload, reference

    source = _mapping(snapshot, "snapshot")
    if "payload" in source:
        payload = deepcopy(dict(_mapping(source["payload"], "snapshot.payload")))
        snapshot_id = source.get("snapshot_id")
        artifact_sha256 = source.get("artifact_sha256")
        artifact_path = source.get("artifact_path")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            snapshot_id = sha256_bytes(canonical_json_bytes(payload, trailing_newline=False))
        if not isinstance(artifact_sha256, str) or not artifact_sha256:
            artifact_sha256 = snapshot_id
        reference = {"snapshot_id": snapshot_id, "artifact_sha256": artifact_sha256}
        if isinstance(artifact_path, str) and artifact_path:
            reference["artifact_path"] = artifact_path
        return payload, reference

    payload = deepcopy(dict(source))
    reference = {
        "snapshot_id": sha256_bytes(canonical_json_bytes(payload, trailing_newline=False)),
        "artifact_sha256": None,
    }
    return payload, reference


def recompute_candidate_scores(
    snapshot: Mapping[str, Any] | str | Path,
    candidate_config: Mapping[str, Any],
    *,
    raw_input_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Recompute scores from frozen snapshot inputs and the supplied candidate config."""
    payload, _ = _snapshot_input(snapshot)
    if payload.get("artifact_type") != "kinace_v3_feature_snapshot":
        raise ScoreReplayError("snapshot artifact has the wrong type")
    candidate = validate_candidate_config(candidate_config)
    weights = candidate["weights"]
    directions = candidate["feature_directions"]
    weight_sum = sum(weights.values())
    effective_weights = {name: weights[name] / weight_sum for name in FEATURE_NAMES}
    penalties = candidate["penalties"]

    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ScoreReplayError("snapshot rows must be a list")

    normalized_rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, f"snapshot.rows[{index}]")
        code = row.get("code")
        decision_date = row.get("decision_date")
        if not isinstance(code, str) or not code:
            raise ScoreReplayError(f"snapshot.rows[{index}].code is required")
        if not isinstance(decision_date, str) or not decision_date:
            raise ScoreReplayError(f"snapshot.rows[{index}].decision_date is required")

        raw_inputs = _mapping(
            row.get("raw_inputs"), f"snapshot.rows[{index}].raw_inputs"
        )
        raw_inputs = apply_raw_input_overrides(
            raw_inputs,
            (raw_input_overrides or {}).get(code),
            FEATURE_NAMES,
        )
        standardization_source = row.get("standardization_inputs")
        standardization_inputs = (
            dict(
                _mapping(
                    standardization_source,
                    f"snapshot.rows[{index}].standardization_inputs",
                )
            )
            if standardization_source is not None
            else {}
        )
        if "market_cap" in standardization_inputs:
            standardization_inputs["market_cap"] = _optional_number(
                standardization_inputs.get("market_cap"),
                f"snapshot.rows[{index}].standardization_inputs.market_cap",
            )
        normalized_rows.append(
            {
                "code": code,
                "decision_date": decision_date,
                "raw_inputs": {
                    name: _optional_number(
                        raw_inputs.get(name),
                        f"snapshot.rows[{index}].raw_inputs.{name}",
                    )
                    for name in FEATURE_NAMES
                },
                "standardization_inputs": standardization_inputs,
            }
        )

    z_score_rows = compute_z_score_inputs(
        normalized_rows,
        weights,
        candidate["standardization"],
    )

    scores: list[dict[str, Any]] = []
    for index, row in enumerate(normalized_rows):
        code = str(row["code"])
        decision_date = str(row["decision_date"])

        z_score_inputs = z_score_rows[index]
        penalty_inputs = _mapping(
            _mapping(rows[index], "snapshot row").get("penalty_inputs"),
            f"snapshot.rows[{index}].penalty_inputs",
        )
        missing_penalties = [
            name for name in PENALTY_INPUT_NAMES if penalty_inputs.get(name) is None
        ]
        if missing_penalties:
            raise ScoreReplayError(
                f"{decision_date}/{code} has missing penalty inputs: {', '.join(missing_penalties)}"
            )
        missing_features = [
            name for name in FEATURE_NAMES if z_score_inputs.get(name) is None
        ]
        if missing_features:
            raise ScoreReplayError(
                f"{decision_date}/{code} has missing z-score inputs: {', '.join(missing_features)}"
            )

        components = {
            name: effective_weights[name]
            * float(directions[name])
            * _finite_number(z_score_inputs[name], f"z_score_inputs.{name}")
            for name in FEATURE_NAMES
        }
        raw_score = sum(components.values())

        rsi = _finite_number(penalty_inputs["rsi"], "penalty_inputs.rsi")
        kdj_j = _finite_number(penalty_inputs["kdj_j"], "penalty_inputs.kdj_j")
        amplitude = _finite_number(
            penalty_inputs["amplitude_5d"], "penalty_inputs.amplitude_5d"
        )
        overheat_penalty = 0.0
        if (
            kdj_j > penalties["kdj_overbought"]
            or rsi > penalties["rsi_overbought"]
        ):
            overheat_penalty = penalties["overheat_penalty"]
        amplitude_penalty = min(
            max(amplitude - penalties["amplitude_threshold"], 0.0)
            * penalties["amplitude_multiplier"],
            penalties["amplitude_max_penalty"],
        )
        adjusted_score = raw_score - overheat_penalty - amplitude_penalty
        score = _stable_logistic(adjusted_score)
        scores.append(
            {
                "decision_date": decision_date,
                "code": code,
                "raw_score": raw_score,
                "adjusted_score": adjusted_score,
                "score": round(score, 6),
                "components": components,
                "penalties": {
                    "overheat": overheat_penalty,
                    "amplitude": amplitude_penalty,
                },
            }
        )

    scores.sort(key=lambda item: (item["decision_date"], item["code"]))
    return scores


def build_replay_artifact(
    snapshot: Mapping[str, Any] | str | Path,
    candidate_config: Mapping[str, Any],
) -> dict[str, Any]:
    payload, source_reference = _snapshot_input(snapshot)
    candidate = validate_candidate_config(candidate_config)
    scores = recompute_candidate_scores(payload, candidate)
    data_gate = deepcopy(dict(_mapping(payload.get("data_gate") or {}, "data_gate")))
    data_gate["parity_claimed"] = False
    return {
        "artifact_type": "kinace_v3_score_replay",
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id_for(candidate),
        "candidate_config": candidate,
        "source_snapshot": source_reference,
        "data_gate": data_gate,
        "replay_semantics": {
            "formula": "direction_adjusted_weighted_z_scores_minus_penalties_logistic",
            "feature_standardization_source": "candidate_config_recomputed_from_raw_inputs",
            "stored_z_score_inputs_reused": False,
            "stored_recommendation_scores_reused": False,
            "real_data_parity_claimed": False,
        },
        "scores": scores,
    }


def write_replay_artifact(
    payload: Mapping[str, Any],
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write a canonical score replay artifact and return its SHA256 replay ID."""
    serialized = canonical_json_bytes(payload)
    replay_id = sha256_bytes(serialized)
    if output_path is None:
        target = (
            Path(__file__).resolve().parents[2]
            / "outputs"
            / "replays"
            / f"{replay_id}.json"
        )
    else:
        target = Path(output_path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(serialized)
    temporary.replace(target)
    return {
        "replay_id": replay_id,
        "artifact_sha256": replay_id,
        "artifact_path": str(target),
        "payload": deepcopy(dict(payload)),
    }


def replay_candidate(
    snapshot: Mapping[str, Any] | str | Path,
    candidate_config: Mapping[str, Any],
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    payload = build_replay_artifact(snapshot, candidate_config)
    return write_replay_artifact(payload, output_path)


def load_replay_artifact(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    target = Path(path)
    actual_sha256 = sha256_file(target)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ScoreReplayError(
            f"replay artifact SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoreReplayError(f"cannot load replay artifact: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScoreReplayError("replay artifact root must be an object")
    if payload.get("artifact_type") != "kinace_v3_score_replay":
        raise ScoreReplayError("artifact is not a Kinace V3 score replay")
    return payload


__all__ = [
    "SCHEMA_VERSION",
    "ScoreReplayError",
    "build_replay_artifact",
    "candidate_id_for",
    "load_replay_artifact",
    "recompute_candidate_scores",
    "replay_candidate",
    "validate_candidate_config",
    "write_replay_artifact",
]
