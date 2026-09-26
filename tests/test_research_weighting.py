from __future__ import annotations

import json

import pytest

from kinace_v3.research_weighting import (
    build_phase4_policy_audit,
    fit_signed_icir_weights,
    project_onto_constrained_simplex,
    standardize_values,
    summarize_ic_series,
    winsorize_mad,
    run_phase4_policy_audit,
)


def test_mad_winsorization_limits_cross_sectional_outliers():
    result = winsorize_mad([0.0, 1.0, 2.0, 3.0, 100.0], 3.0)

    assert result[0] == pytest.approx(0.0)
    assert result[-1] < 10.0
    assert result[-1] > 3.0


def test_ic_summary_preserves_negative_direction_and_stability():
    result = summarize_ic_series([-0.10, -0.08, -0.12, -0.09, -0.11])

    assert result["direction"] == -1
    assert result["mean_ic"] < 0
    assert result["icir"] < 0
    assert result["strength"] > 0
    assert result["sign_consistency"] == 1.0
    assert result["newey_west_t_stat"] < 0


def test_recency_weighted_ic_uses_recent_direction():
    result = summarize_ic_series(
        [-0.08] * 10 + [0.03] * 5,
        recency_halflife=2.0,
    )

    assert result["mean_ic"] < 0
    assert result["direction"] == 1
    assert result["direction_basis"] == "recency_weighted"
    assert result["recency_weighted"]["halflife_sessions"] == 2.0
    assert result["recency_weighted"]["mean_ic"] > 0


def test_recency_direction_only_keeps_full_sample_strength():
    values = [-0.08] * 10 + [0.03] * 5
    full = summarize_ic_series(values)
    result = summarize_ic_series(
        values,
        recency_halflife=2.0,
        recency_mode="direction_only",
    )

    assert result["direction"] == 1
    assert result["direction_basis"] == "recency_weighted"
    assert result["strength"] == full["strength"]
    assert result["strength_basis"] == "full_sample"


def test_conflict_contract_keeps_full_direction_and_shrinks_strength():
    values = [-0.08] * 10 + [0.03] * 5
    full = summarize_ic_series(values)
    result = summarize_ic_series(
        values,
        recency_halflife=2.0,
        recency_mode="conflict_contract",
        conflict_scale=0.25,
    )

    assert result["direction"] == full["direction"]
    assert result["strength"] == pytest.approx(full["strength"] * 0.25)
    assert result["conflict_contract"]["detected"] is True
    assert result["strength_basis"] == "conflict_contracted"


def test_signed_icir_weights_use_negative_factor_and_satisfy_max_weight():
    fitted = fit_signed_icir_weights(
        base_weights={"alpha": 0.5, "beta": 0.3, "gamma": 0.2},
        ic_statistics={
            "alpha": summarize_ic_series([0.08, 0.10, 0.09, 0.11]),
            "beta": summarize_ic_series([-0.09, -0.10, -0.08, -0.11]),
            "gamma": summarize_ic_series([0.0, 0.0, 0.0, 0.0]),
        },
        max_feature_weight=0.60,
    )

    assert fitted["feature_directions"]["beta"] == -1
    assert fitted["trained_weights"]["beta"] > 0
    assert fitted["trained_weights"]["gamma"] > 0
    assert sum(fitted["trained_weights"].values()) == pytest.approx(1.0)
    assert max(fitted["trained_weights"].values()) <= 0.60
    assert fitted["max_feature_weight_satisfied"] is True


def test_constrained_projection_is_feasible_and_deterministic():
    first = project_onto_constrained_simplex([0.9, 0.1, 0.0, 0.0], 0.40)
    second = project_onto_constrained_simplex([0.9, 0.1, 0.0, 0.0], 0.40)

    assert first == second
    assert sum(first) == pytest.approx(1.0)
    assert max(first) == pytest.approx(0.40)
    assert all(value >= 0 for value in first)


def test_standardization_can_neutralize_industry():
    result = standardize_values(
        [10.0, 11.0, 12.0, 30.0, 31.0, 32.0],
        config={
            "version": 1,
            "method": "mad_winsorized",
            "mad_multiplier": 3.0,
            "neutralize_industry": True,
            "neutralize_market_cap": False,
        },
        industries=["a", "a", "a", "b", "b", "b"],
    )

    assert sum(result[:3]) == pytest.approx(0.0)
    assert sum(result[3:]) == pytest.approx(0.0)


def test_phase4_policy_audit_records_negative_ic_and_solver_checks(tmp_path):
    phase05 = {
        "status": "completed",
        "classifications": {"weak_but_same_sign": ["beta"]},
        "findings": ["negative mean IC across all available methods: beta"],
        "factors": [
            {
                "factor": "beta",
                "classification": "weak_but_same_sign",
                "recommendation": "retain_with_sign_control",
                "all_method_sign_consistency": 0.8,
            }
        ],
    }
    loop_report = {
        "rounds": [
            {
                "round": 0,
                "candidates": [
                    {
                        "signature": "legacy-candidate",
                        "folds": [
                            {
                                "fold": 0,
                                "training": {
                                    "mean_rank_ic": {"alpha": 0.02, "beta": -0.03},
                                    "trained_weights": {"alpha": 0.8, "beta": 0.2},
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    audit = build_phase4_policy_audit(
        phase05_report=phase05,
        loop_report=loop_report,
        max_feature_weight=0.6,
    )

    assert audit["status"] == "completed_method"
    assert audit["solver_verification"]["passed"] is True
    assert audit["legacy_candidate_evidence"]["negative_direction_handling_changed"] is True
    assert audit["data_dependent_rerun_required"] is True

    phase05_path = tmp_path / "phase05.json"
    loop_path = tmp_path / "loop.json"
    phase05_path.write_text(json.dumps(phase05), encoding="utf-8")
    loop_path.write_text(json.dumps(loop_report), encoding="utf-8")
    written = run_phase4_policy_audit(
        phase05_report_path=phase05_path,
        loop_report_path=loop_path,
        max_feature_weight=0.6,
        output_path=tmp_path / "phase4.json",
        markdown_path=tmp_path / "phase4.md",
    )

    assert (tmp_path / "phase4.json").is_file()
    assert (tmp_path / "phase4.md").is_file()
    assert written["artifact_sha256"]
