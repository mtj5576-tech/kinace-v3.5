"""Point-in-time feature transforms shared by training and score replay."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Mapping, Sequence


TRANSFORM_VERSION = 1
SUPPORTED_TRANSFORMS = {"change"}
SUPPORTED_FEATURES = {"log_amount"}


class ResearchFeatureTransformError(ValueError):
    """Raised when a configured feature transform is invalid."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchFeatureTransformError(f"{field} must be an object")
    return value


def _finite_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchFeatureTransformError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchFeatureTransformError(f"{field} must be a finite number")
    return result


def normalize_feature_transform(
    config: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if config is None:
        return {}
    source = _mapping(config, "feature_transform")
    unknown_features = sorted(set(source) - SUPPORTED_FEATURES)
    if unknown_features:
        raise ResearchFeatureTransformError(
            "unsupported feature transforms: " + ", ".join(unknown_features)
        )
    result: dict[str, dict[str, Any]] = {}
    for feature, raw_policy in source.items():
        policy = dict(_mapping(raw_policy, f"feature_transform.{feature}"))
        unknown_keys = sorted(
            set(policy) - {"version", "mode", "lookback_sessions"}
        )
        if unknown_keys:
            raise ResearchFeatureTransformError(
                f"unknown feature_transform.{feature} keys: "
                + ", ".join(unknown_keys)
            )
        mode = str(policy.get("mode") or "").strip().lower()
        if mode not in SUPPORTED_TRANSFORMS:
            raise ResearchFeatureTransformError(
                f"feature_transform.{feature}.mode is unsupported"
            )
        lookback = policy.get("lookback_sessions")
        version = policy.get("version", TRANSFORM_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) or version != TRANSFORM_VERSION:
            raise ResearchFeatureTransformError(
                f"feature_transform.{feature}.version must be {TRANSFORM_VERSION}"
            )
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback <= 0:
            raise ResearchFeatureTransformError(
                f"feature_transform.{feature}.lookback_sessions must be positive"
            )
        result[str(feature)] = {
            "version": TRANSFORM_VERSION,
            "mode": mode,
            "lookback_sessions": int(lookback),
        }
    return result


class FeatureTransformState:
    """Apply deterministic transforms using only previously seen snapshots."""

    def __init__(self, config: Mapping[str, Any] | None) -> None:
        self.policy = normalize_feature_transform(config)
        self._history: dict[str, dict[str, list[float]]] = {
            feature: defaultdict(list) for feature in self.policy
        }
        self._seen_dates: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.policy)

    def apply(
        self,
        decision_date: str,
        payload: Mapping[str, Any],
    ) -> dict[str, dict[str, float | None]]:
        if not self.policy:
            return {}
        if decision_date in self._seen_dates:
            raise ResearchFeatureTransformError(
                f"decision date is not strictly increasing: {decision_date}"
            )
        self._seen_dates.add(decision_date)
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ResearchFeatureTransformError("snapshot rows must be a list")
        overrides: dict[str, dict[str, float | None]] = {}
        for index, raw_row in enumerate(rows):
            row = _mapping(raw_row, f"snapshot.rows[{index}]")
            code = str(row.get("code") or "")
            if not code:
                raise ResearchFeatureTransformError(
                    f"snapshot.rows[{index}].code is required"
                )
            raw_inputs = _mapping(
                row.get("raw_inputs"), f"snapshot.rows[{index}].raw_inputs"
            )
            code_override: dict[str, float | None] = {}
            for feature, policy in self.policy.items():
                current = _finite_number(
                    raw_inputs.get(feature),
                    f"snapshot.rows[{index}].raw_inputs.{feature}",
                )
                history = self._history[feature][code]
                lookback = int(policy["lookback_sessions"])
                code_override[feature] = (
                    current - history[-lookback]
                    if current is not None and len(history) >= lookback
                    else None
                )
                if current is not None:
                    history.append(current)
            overrides[code] = code_override
        return overrides


def apply_raw_input_overrides(
    raw_inputs: Mapping[str, Any],
    overrides: Mapping[str, Any] | None,
    features: Sequence[str],
) -> dict[str, Any]:
    """Return raw inputs with transformed values, falling back when unavailable."""
    result = dict(raw_inputs)
    if not isinstance(overrides, Mapping):
        return result
    for feature in features:
        value = overrides.get(feature)
        if value is not None:
            result[feature] = value
    return result


__all__ = [
    "FeatureTransformState",
    "ResearchFeatureTransformError",
    "apply_raw_input_overrides",
    "normalize_feature_transform",
]
