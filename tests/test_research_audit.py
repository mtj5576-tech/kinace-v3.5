from __future__ import annotations

from kinace_v3.research_audit import audit_execution, build_audit_report


def _report() -> dict:
    entry_price = 10.005
    exit_price = 10.9945
    shares = 1_000
    entry_gross = entry_price * shares
    exit_gross = exit_price * shares
    entry_fees = max(5.0, entry_gross * 0.0003) + entry_gross * 0.00001
    exit_fees = max(5.0, exit_gross * 0.0003) + exit_gross * (0.00001 + 0.0005)
    return {
        "config": {
            "initial_cash": 100_000.0,
            "max_positions": 2,
            "position_size_pct": 0.5,
            "lot_size": 100,
            "min_score": 0.0,
            "max_hold_sessions": 10,
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "transfer_fee_rate": 0.00001,
            "slippage_bps": 5.0,
            "stop_atr_multiple": 2.0,
            "target_atr_multiple": 3.0,
            "atr_period": 14,
        },
        "trades": [
            {
                "code": "000001",
                "entry_date": "2026-01-05",
                "exit_date": "2026-01-06",
                "intended_entry": 10.0,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "shares": shares,
                "fees": entry_fees + exit_fees,
            }
        ],
    }


def _bars() -> dict[str, list[dict]]:
    return {
        "000001": [
            {
                "trade_date": "2026-01-05",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000_000.0,
                "is_suspended": False,
                "is_limit_up": False,
                "is_limit_down": False,
            },
            {
                "trade_date": "2026-01-06",
                "open": 11.0,
                "high": 11.1,
                "low": 10.8,
                "close": 11.0,
                "volume": 1_000_000.0,
                "is_suspended": False,
                "is_limit_up": False,
                "is_limit_down": False,
            },
        ]
    }


def test_execution_audit_replays_price_slippage_and_fees():
    result = audit_execution(_report(), _bars())

    assert result["trade_count"] == 1
    assert result["violation_count"] == 0


def test_execution_audit_rejects_non_next_session_entry():
    report = _report()
    report["trades"][0].update(
        {
            "decision_date": "2026-01-05",
            "stop_loss": 9.0,
            "take_profit": 12.0,
            "exit_reason": "target",
        }
    )

    result = audit_execution(report, _bars())

    assert any(
        item["type"] == "entry_not_next_session"
        for item in result["violations"]
    )


def test_audit_report_marks_data_and_universe_risks_as_critical():
    execution = audit_execution(_report(), _bars())
    report = build_audit_report(
        price={
            "severe_jump_count": 1,
            "suspicious_jump_count": 1,
            "suspicious_jumps": [{"ts_code": "300806", "trade_date": "2024-08-23"}],
        },
        parity={
            "passed": True,
            "max_delta": {"close_return": 0.3182},
            "mismatch_ratio": 0.0073,
            "high_return_mismatches": [],
        },
        state={"missing_state_count": 0},
        execution=execution,
        universe={
            "historical_universe_verifiable": False,
            "metadata_count": 62,
            "list_date_present": 0,
            "delist_date_present": 0,
            "sector_membership_records": 0,
            "index_member_records": 0,
        },
    )

    assert report["status"] == "review_required"
    assert report["severity_counts"]["critical"] >= 2
    assert {item["code"] for item in report["findings"]} >= {
        "price_discontinuity",
        "source_parity_large_delta",
        "unverifiable_historical_universe",
    }


def test_preflight_audit_does_not_require_candidate_execution():
    execution = {
        "status": "not_applicable",
        "trade_count": 0,
        "violation_count": 0,
        "violations": [],
    }
    report = build_audit_report(
        price={"severe_jump_count": 0, "suspicious_jump_count": 0},
        parity={"passed": True, "max_delta": {"close_return": 0.0}},
        state={"missing_state_count": 0},
        execution=execution,
        universe={"historical_universe_verifiable": True},
        binding={"snapshot_set_id": "snapshot-set"},
        execution_applicable=False,
    )

    assert report["status"] == "passed"
    assert report["research_eligible"] is True
    assert report["candidate_bound"] is False
    assert report["candidate_audit_passed"] is False


def test_candidate_audit_binding_marks_bound_candidate_audit_passed():
    binding = {
        "loop_id": "loop-1",
        "candidate_signature": "candidate-1",
        "snapshot_set_id": "snapshot-set-1",
        "holdout_run_id": "run-1",
        "replay_id": "replay-1",
        "experiment_id": "experiment-1",
        "evidence_id": "evidence-1",
    }
    report = build_audit_report(
        price={"severe_jump_count": 0, "suspicious_jump_count": 0},
        parity={"passed": True, "max_delta": {"close_return": 0.0}},
        state={"missing_state_count": 0},
        execution={"trade_count": 1, "violation_count": 0, "violations": []},
        universe={"historical_universe_verifiable": True},
        binding=binding,
        execution_applicable=True,
    )

    assert report["candidate_bound"] is True
    assert report["candidate_audit_passed"] is True
    assert report["binding"] == binding
