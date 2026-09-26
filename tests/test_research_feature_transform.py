from __future__ import annotations

import pytest

from kinace_v3.research_feature_transform import (
    FeatureTransformState,
    ResearchFeatureTransformError,
    apply_raw_input_overrides,
)


def _payload(decision_date: str, rows: list[dict]) -> dict:
    return {"decision_date": decision_date, "rows": rows}


def test_change_transform_uses_only_prior_snapshot_history():
    state = FeatureTransformState(
        {
            "log_amount": {
                "mode": "change",
                "lookback_sessions": 1,
            }
        }
    )

    first = state.apply(
        "2026-01-02",
        _payload(
            "2026-01-02",
            [{"code": "000001", "raw_inputs": {"log_amount": 1.0}}],
        ),
    )
    second = state.apply(
        "2026-01-03",
        _payload(
            "2026-01-03",
            [{"code": "000001", "raw_inputs": {"log_amount": 2.5}}],
        ),
    )

    assert first == {"000001": {"log_amount": None}}
    assert second == {"000001": {"log_amount": 1.5}}


def test_runtime_overrides_fall_back_when_transform_is_not_ready():
    raw = {"log_amount": 3.0, "ret_5": 0.01}

    transformed = apply_raw_input_overrides(
        raw,
        {"log_amount": 1.25},
        ("log_amount", "ret_5"),
    )
    fallback = apply_raw_input_overrides(
        raw,
        {"log_amount": None},
        ("log_amount", "ret_5"),
    )

    assert transformed["log_amount"] == 1.25
    assert fallback["log_amount"] == 3.0


def test_feature_transform_rejects_unknown_mode():
    with pytest.raises(ResearchFeatureTransformError):
        FeatureTransformState(
            {
                "log_amount": {
                    "mode": "unsupported",
                    "lookback_sessions": 5,
                }
            }
        )
