from __future__ import annotations

import sqlite3
from copy import deepcopy

from kinace_v3.research_loop import DEFAULT_WALK_FORWARD
from kinace_v3.research_protocol import (
    EXECUTION_SEMANTICS,
    REQUIRED_PROTOCOL_FIELDS,
    archive_active_challengers,
    build_effective_research_protocol,
    protocol_candidate_id,
    recompute_evaluation_protocol_hash,
)


def _model_config() -> dict:
    return {
        "version": "protocol-test",
        "mode": "research_loop_test",
        "weights": {
            "rel_sector_strength": 0.20,
            "close_location_5": 0.15,
            "volume_ratio": 0.15,
            "log_amount": 0.15,
            "turnover": 0.10,
            "proximity_20d_high": 0.10,
            "ret_5": 0.10,
            "gap": 0.05,
        },
        "penalties": {
            "rsi_overbought": 80.0,
            "kdj_overbought": 100.0,
            "overheat_penalty": 1.0,
            "amplitude_threshold": 0.04,
            "amplitude_multiplier": 4.0,
            "amplitude_max_penalty": 0.35,
        },
    }


def _backtest_config() -> dict:
    return {
        "initial_cash": 100_000.0,
        "max_positions": 1,
        "position_size_pct": 0.5,
        "max_hold_sessions": 2,
        "min_score": 0.0,
    }


def _protocol(
    *,
    max_feature_weight: float = 0.75,
    candidate_min_trade_count: int = 30,
    holdout_max_sessions: int = 120,
) -> dict:
    model = _model_config()
    backtest = _backtest_config()
    return build_effective_research_protocol(
        loop_id="loop-test",
        code_version="3.3.0",
        objective={
            "hard_gates": {
                "min_trade_count": 30,
                "max_drawdown": 0.60,
            }
        },
        walk_forward={
            **DEFAULT_WALK_FORWARD,
            "purge_sessions": 5,
            "embargo_sessions": 5,
        },
        stability_config={
            "max_feature_weight": max_feature_weight,
            "candidate_min_trade_count": candidate_min_trade_count,
        },
        seeds=[
            {
                "signature": "candidate-signature",
                "model_config": model,
                "backtest_config": backtest,
            }
        ],
        model_search_space={},
        backtest_search_space={},
        walk_forward_defaults=DEFAULT_WALK_FORWARD,
        holdout_policy={"max_total_sessions": holdout_max_sessions},
    )


def test_evaluation_protocol_hash_is_stable_and_detects_config_change():
    first = _protocol()
    second = _protocol()
    changed = _protocol(max_feature_weight=0.70)
    changed_trade_count = _protocol(candidate_min_trade_count=31)
    changed_holdout = _protocol(holdout_max_sessions=121)

    assert first["evaluation_protocol_hash"] == second["evaluation_protocol_hash"]
    assert first["evaluation_protocol_hash"] != changed["evaluation_protocol_hash"]
    assert (
        first["evaluation_protocol_hash"]
        != changed_trade_count["evaluation_protocol_hash"]
    )
    assert first["evaluation_protocol_hash"] != changed_holdout["evaluation_protocol_hash"]
    assert (
        recompute_evaluation_protocol_hash(first)
        == first["evaluation_protocol_hash"]
    )
    assert set(REQUIRED_PROTOCOL_FIELDS) == set(
        first["required_evaluation_fields"]
    )
    assert (
        first["required_evaluation_fields"]["min_observation_sessions"]
        == 0
    )
    assert EXECUTION_SEMANTICS["equity_basis"] == "mark_to_market"
    assert EXECUTION_SEMANTICS["equity_curve"] == "mark_to_market_equity"
    assert EXECUTION_SEMANTICS["price_basis"] == "raw"
    assert EXECUTION_SEMANTICS["execution_price_basis"] == "raw"
    assert EXECUTION_SEMANTICS["research_price_basis"] == "total_return"


def test_protocol_hash_changes_when_execution_price_basis_changes():
    protocol = _protocol()
    changed = deepcopy(protocol)
    changed["protocol_identity"]["evaluation_config"]["execution_semantics"][
        "price_basis"
    ] = "adjusted"

    assert (
        recompute_evaluation_protocol_hash(protocol)
        != recompute_evaluation_protocol_hash(changed)
    )


def test_protocol_binds_the_shared_strategy_and_benchmark_anomaly_policy():
    protocol = _protocol()
    anomaly_policy = EXECUTION_SEMANTICS["return_anomaly_policy"]

    assert anomaly_policy["strategy"] == "retain_all_finite_returns"
    assert anomaly_policy["benchmark"] == "retain_all_finite_returns"
    assert anomaly_policy["sanitized_auxiliary"]["role"] == "audit_only"
    assert (
        protocol["protocol_identity"]["evaluation_config"]["execution_semantics"][
            "return_anomaly_policy"
        ]
        == anomaly_policy
    )

    changed = deepcopy(protocol)
    changed["protocol_identity"]["evaluation_config"]["execution_semantics"][
        "return_anomaly_policy"
    ]["benchmark"] = "different_policy"
    assert (
        recompute_evaluation_protocol_hash(protocol)
        != recompute_evaluation_protocol_hash(changed)
    )


def test_protocol_candidate_id_binds_protocol_and_candidate():
    protocol_hash = _protocol()["evaluation_protocol_hash"]

    assert protocol_candidate_id(protocol_hash, "candidate-a") != protocol_candidate_id(
        protocol_hash, "candidate-b"
    )
    assert protocol_candidate_id(protocol_hash, "candidate-a") != protocol_candidate_id(
        "different-protocol", "candidate-a"
    )


def test_archive_active_challengers_records_terminal_state(tmp_path):
    database = tmp_path / "research.sqlite3"
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            """
            CREATE TABLE research_candidates (
                candidate_signature TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                latest_loop_id TEXT NOT NULL,
                holdout_status TEXT,
                superseded_by_loop_id TEXT,
                superseded_reason TEXT,
                superseded_at_utc TEXT,
                updated_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO research_candidates (
                candidate_signature, stage, latest_loop_id,
                holdout_status, updated_at_utc
            ) VALUES ('candidate-a', 'audit_passed_candidate', 'loop-old',
                      'challenger_ready', '2026-09-19T00:00:00Z')
            """
        )

    result = archive_active_challengers(
        database,
        superseded_by_loop_id="loop-new",
        reason="phase0-test",
    )

    assert result["status"] == "archived"
    assert result["superseded_candidates"][0]["candidate_signature"] == "candidate-a"
    with sqlite3.connect(str(database)) as connection:
        row = connection.execute(
            """
            SELECT stage, superseded_by_loop_id, superseded_reason
            FROM research_candidates WHERE candidate_signature='candidate-a'
            """
        ).fetchone()
    assert row == ("superseded", "loop-new", "phase0-test")
