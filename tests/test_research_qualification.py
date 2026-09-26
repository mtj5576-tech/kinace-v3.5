from __future__ import annotations

import json

from kinace_v3.research_qualification import _statistical_decision_binding


def _artifacts(tmp_path):
    statistics_path = tmp_path / "statistics.json"
    decision_path = tmp_path / "decision.json"
    statistics = {
        "artifact_type": "kinace_v3_research_statistics",
        "loop_id": "loop-1",
        "run_id": "holdout-run",
        "candidate_id": "model-candidate",
        "phase_1b_ready": True,
        "trial_ledger": {
            "ledger_accounting_complete": True,
            "N_trials_total": 11,
            "N_trials_total_recorded": 1,
            "unrecorded_trials_upper_bound": 10,
            "ledger_complete": False,
            "ledger_hash": "ledger-sha",
        },
        "statistics": {
            "sample_requirements": {
                "required_sessions": 120,
                "sessions_met": True,
                "required_trades": 30,
                "trades_met": True,
            },
            "bootstrap_confidence_interval": {
                "annualized_sharpe": {"lower": 0.5, "upper": 1.5}
            },
            "deflated_sharpe_ratio": {"computable": True, "p_value": 0.01},
            "target_power": {"N_eff_measured": 500.0, "N_eff_required": 400.0},
            "binding_gates": {
                "passed": True,
                "checks": {
                    "bootstrap_confidence_interval_computable": True,
                    "bootstrap_confidence_interval_lower_bound_positive": True,
                    "deflated_sharpe_computable": True,
                    "deflated_sharpe_p_value_below_0_05": True,
                    "effective_sample_size_meets_target": True,
                    "minimum_observation_sessions_established": True,
                    "minimum_observation_sessions_met": True,
                    "minimum_trades_established": True,
                    "minimum_trades_met": True,
                },
            },
        },
    }
    decision = {
        "artifact_type": "kinace_v3_provisional_phase1b_decision",
        "loop_id": "loop-1",
        "candidate_signature": "candidate-1",
        "status": "provisional_passed",
        "trial_ledger": {"ledger_hash": "ledger-sha"},
        "checks": {"all_required_statistical_gates_met": True},
    }
    statistics_path.write_text(json.dumps(statistics), encoding="utf-8")
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    return statistics, decision, statistics_path, decision_path


def test_statistical_decision_binding_requires_passed_statistics_and_decision(tmp_path):
    statistics, decision, statistics_path, decision_path = _artifacts(tmp_path)
    loop_report = {
        "loop_id": "loop-1",
        "best_candidate": {"signature": "candidate-1"},
        "holdout": {"portfolio_run_id": "holdout-run"},
    }
    candidate_audit = {
        "status": "passed",
        "candidate_audit_passed": True,
        "binding": {"model_candidate_id": "model-candidate"},
    }

    passed = _statistical_decision_binding(
        statistics=statistics,
        decision=decision,
        loop_report=loop_report,
        candidate_audit=candidate_audit,
        statistics_path=statistics_path,
        decision_path=decision_path,
    )

    assert passed["status"] == "passed"
    assert all(passed["checks"].values())

    statistics["statistics"]["bootstrap_confidence_interval"]["annualized_sharpe"][
        "lower"
    ] = -0.01
    statistics["statistics"]["binding_gates"]["passed"] = False
    statistics["statistics"]["binding_gates"]["checks"][
        "bootstrap_confidence_interval_lower_bound_positive"
    ] = False
    statistics_path.write_text(json.dumps(statistics), encoding="utf-8")

    failed = _statistical_decision_binding(
        statistics=statistics,
        decision=decision,
        loop_report=loop_report,
        candidate_audit=candidate_audit,
        statistics_path=statistics_path,
        decision_path=decision_path,
    )

    assert failed["status"] == "failed"
    assert failed["checks"]["statistics_bootstrap_ci_lower_positive"] is False
