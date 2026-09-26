from __future__ import annotations

import pytest

from kinace_v3.score_replay import (
    ScoreReplayError,
    apply_score_persistence,
)


def test_score_persistence_blends_only_prior_scores():
    scores = [
        {"decision_date": "2026-01-01", "code": "000001", "adjusted_score": 0.0},
        {"decision_date": "2026-01-02", "code": "000001", "adjusted_score": 1.0},
        {"decision_date": "2026-01-03", "code": "000001", "adjusted_score": 2.0},
    ]

    result = apply_score_persistence(
        scores,
        {
            "persistence_sessions": 2,
            "persistence_weight": 0.5,
        },
    )

    assert result[2]["adjusted_score"] == pytest.approx(1.25)
    assert result[2]["persistence"]["applied"] is True
    assert result[0]["persistence"]["applied"] is False


def test_score_persistence_rejects_invalid_weight():
    with pytest.raises(ScoreReplayError):
        apply_score_persistence(
            [],
            {
                "persistence_sessions": 3,
                "persistence_weight": 1.5,
            },
        )
