from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from kinace_v3.experiments import (
    join_lineage,
    record_backtest_evidence,
    record_experiment,
    record_replay,
)
from kinace_v3.feature_snapshot import (
    FEATURE_NAMES,
    MODEL_PENALTY_NAMES,
    canonical_json_bytes,
    create_feature_snapshot,
    persist_feature_snapshot,
    sha256_file,
    write_feature_snapshot,
)
from kinace_v3.score_replay import (
    ScoreReplayError,
    candidate_id_for,
    load_replay_artifact,
    replay_candidate,
)


def _model_config(version: str = "synthetic-base") -> dict:
    return {
        "version": version,
        "mode": "synthetic_test",
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


def _candidate_config(feature: str, version: str) -> dict:
    config = _model_config(version)
    config["weights"] = {name: (1.0 if name == feature else 0.0) for name in FEATURE_NAMES}
    return config


def _synthetic_candidates() -> list[dict]:
    feature_values = {
        "000001": [0.01, 0.10, 0.50, 6.00, 0.20, 0.70, -0.01, 0.03],
        "000002": [0.02, 0.20, 0.80, 6.50, 0.40, 0.80, 0.01, 0.00],
        "000003": [0.03, 0.30, 1.10, 7.00, 0.60, 0.90, 0.02, 0.02],
        "000004": [0.04, 0.40, 1.40, 7.50, 0.80, 1.00, 0.03, 0.01],
    }
    rows = []
    for code, values in feature_values.items():
        raw_inputs = dict(zip(FEATURE_NAMES, values))
        rows.append(
            {
                "code": code,
                "raw_inputs": raw_inputs,
                "penalty_inputs": {
                    "rsi": 55.0,
                    "kdj_j": 70.0,
                    "amplitude_5d": 0.02,
                },
                "labels": {"forward_return": 0.01},
                "stored_recommendation_score": 99.99,
                "point_in_time": {"as_of_utc": "2024-01-02T07:00:00Z"},
            }
        )
    return rows


def _written_snapshot(tmp_path: Path) -> dict:
    payload = create_feature_snapshot(
        decision_date="2024-01-02",
        candidates=_synthetic_candidates(),
        model_config=_model_config(),
        data_gate={"status": "blocked_insufficient_point_in_time_data", "ready": False},
    )
    return write_feature_snapshot(payload, tmp_path / "snapshot.json")


def test_two_candidate_configurations_produce_different_replay_scores(tmp_path):
    snapshot = _written_snapshot(tmp_path)
    assert snapshot["artifact_sha256"] == sha256_file(snapshot["artifact_path"])
    assert snapshot["payload"]["data_gate"]["parity_claimed"] is False
    assert all(row["labels"] == {"forward_return": 0.01} for row in snapshot["payload"]["rows"])
    assert all(
        set(row["raw_inputs"]) >= set(FEATURE_NAMES)
        and set(row["z_score_inputs"]) == set(FEATURE_NAMES)
        and set(row["penalty_inputs"]) >= {"rsi", "kdj_j", "amplitude_5d"}
        and set(row["model_config"]["penalties"]) == set(MODEL_PENALTY_NAMES)
        for row in snapshot["payload"]["rows"]
    )

    config_a = _candidate_config("rel_sector_strength", "synthetic-a")
    config_b = _candidate_config("gap", "synthetic-b")
    replay_a = replay_candidate(snapshot, config_a, tmp_path / "replay-a.json")
    replay_b = replay_candidate(snapshot, config_b, tmp_path / "replay-b.json")

    scores_a = [row["score"] for row in replay_a["payload"]["scores"]]
    scores_b = [row["score"] for row in replay_b["payload"]["scores"]]
    assert scores_a != scores_b
    assert replay_a["replay_id"] == sha256_file(replay_a["artifact_path"])
    assert replay_b["replay_id"] == sha256_file(replay_b["artifact_path"])
    assert replay_a["payload"]["replay_semantics"] == {
        "formula": "direction_adjusted_weighted_z_scores_minus_penalties_logistic",
        "feature_standardization_source": "candidate_config_recomputed_from_raw_inputs",
        "stored_z_score_inputs_reused": False,
        "stored_recommendation_scores_reused": False,
        "real_data_parity_claimed": False,
    }
    assert all(item["score"] != 99.99 for item in replay_a["payload"]["scores"])

    loaded = load_replay_artifact(
        replay_a["artifact_path"], expected_sha256=replay_a["artifact_sha256"]
    )
    assert canonical_json_bytes(loaded) == Path(replay_a["artifact_path"]).read_bytes()


def test_replay_recomputes_standardization_instead_of_reusing_stored_z_scores(tmp_path):
    snapshot = _written_snapshot(tmp_path)
    candidate = _candidate_config("gap", "synthetic-standardization-source")
    baseline = replay_candidate(snapshot, candidate, tmp_path / "baseline.json")

    tampered = deepcopy(snapshot)
    for row in tampered["payload"]["rows"]:
        row["z_score_inputs"] = {name: 1000.0 for name in FEATURE_NAMES}
    replayed = replay_candidate(tampered, candidate, tmp_path / "tampered.json")

    assert [row["raw_score"] for row in replayed["payload"]["scores"]] == [
        row["raw_score"] for row in baseline["payload"]["scores"]
    ]
    assert [row["score"] for row in replayed["payload"]["scores"]] == [
        row["score"] for row in baseline["payload"]["scores"]
    ]


def test_candidate_feature_direction_reverses_factor_contribution(tmp_path):
    snapshot = _written_snapshot(tmp_path)
    positive = _candidate_config("gap", "synthetic-gap-positive")
    negative = deepcopy(positive)
    negative["version"] = "synthetic-gap-negative"
    negative["feature_directions"] = {"gap": -1}

    positive_replay = replay_candidate(snapshot, positive, tmp_path / "positive.json")
    negative_replay = replay_candidate(snapshot, negative, tmp_path / "negative.json")
    positive_rank = [
        row["code"]
        for row in sorted(
            positive_replay["payload"]["scores"],
            key=lambda item: (-item["score"], item["code"]),
        )
    ]
    negative_rank = [
        row["code"]
        for row in sorted(
            negative_replay["payload"]["scores"],
            key=lambda item: (-item["score"], item["code"]),
        )
    ]

    assert positive_rank == list(reversed(negative_rank))


def test_replay_is_deterministic_for_the_same_snapshot_and_candidate(tmp_path):
    snapshot = _written_snapshot(tmp_path)
    candidate = _candidate_config("ret_5", "synthetic-deterministic")

    first = replay_candidate(snapshot, candidate, tmp_path / "first.json")
    second = replay_candidate(snapshot, candidate, tmp_path / "second.json")

    assert first["replay_id"] == second["replay_id"]
    assert first["payload"] == second["payload"]
    assert Path(first["artifact_path"]).read_bytes() == Path(second["artifact_path"]).read_bytes()


def test_snapshot_preserves_missing_inputs_and_replay_fails_closed(tmp_path):
    candidates = _synthetic_candidates()
    del candidates[0]["penalty_inputs"]["rsi"]
    payload = create_feature_snapshot(
        decision_date="2024-01-02",
        candidates=candidates,
        model_config=_model_config(),
    )
    snapshot = write_feature_snapshot(payload, tmp_path / "missing.json")

    assert "rsi" in snapshot["payload"]["rows"][0]["penalty_inputs"]["missing_fields"]
    with pytest.raises(ScoreReplayError, match="missing penalty inputs"):
        replay_candidate(snapshot, _candidate_config("gap", "synthetic-missing"))


def test_lineage_joins_candidate_replay_and_backtest_by_ids(tmp_path):
    database = tmp_path / "kinace_v3_2.sqlite3"
    snapshot = _written_snapshot(tmp_path)
    persist_feature_snapshot(snapshot, database)
    candidate = _candidate_config("gap", "synthetic-lineage")
    replay = replay_candidate(snapshot, candidate, tmp_path / "lineage-replay.json")
    replay_id = record_replay(database, replay, code_version="kinace-v3-test")

    search_space = {"weights.gap": [0.0, 1.0], "seed": [1]}
    objective_config = {"metric": "rank_ic", "direction": "maximize"}
    artifact_paths = {
        "snapshot": snapshot["artifact_path"],
        "replay": replay["artifact_path"],
    }
    experiment_id = record_experiment(
        database,
        code_version="kinace-v3-test",
        data_snapshot_hash=snapshot["artifact_sha256"],
        search_space=search_space,
        seed=7,
        objective_config=objective_config,
        candidate_id=replay["payload"]["candidate_id"],
        replay_id=replay_id,
        artifact_paths=artifact_paths,
    )

    backtest_artifact = tmp_path / "backtest.json"
    backtest_artifact.write_bytes(canonical_json_bytes({"equity": [1.0, 1.01]}))
    backtest_id = record_backtest_evidence(
        database,
        code_version="kinace-v3-test",
        candidate_id=replay["payload"]["candidate_id"],
        replay_id=replay_id,
        run_id="synthetic-run-1",
        metrics={"rank_ic": 0.12, "trades": 3},
        artifact_path=backtest_artifact,
    )

    lineage = join_lineage(database, experiment_id=experiment_id)
    assert len(lineage) == 1
    joined = lineage[0]
    assert joined["candidate"]["candidate_id"] == candidate_id_for(candidate)
    assert joined["replay"]["replay_id"] == replay_id
    assert joined["replay"]["snapshot_id"] == snapshot["snapshot_id"]
    assert joined["experiment"]["data_snapshot_hash"] == snapshot["artifact_sha256"]
    assert joined["backtest"]["backtest_id"] == backtest_id
    assert joined["backtest"]["run_id"] == "synthetic-run-1"
    assert joined["backtest"]["metrics"]["trades"] == 3

    with sqlite3.connect(str(database)) as connection:
        stored = connection.execute(
            """
            SELECT e.candidate_id, e.replay_id, r.data_snapshot_hash
            FROM experiments e JOIN score_replays r USING (replay_id)
            WHERE e.experiment_id=?
            """,
            (experiment_id,),
        ).fetchone()
    assert stored == (replay["payload"]["candidate_id"], replay_id, snapshot["artifact_sha256"])


def test_replay_candidate_does_not_inherit_snapshot_model_version(tmp_path):
    snapshot = _written_snapshot(tmp_path)
    candidate = deepcopy(_model_config("different-candidate-version"))
    replay = replay_candidate(snapshot, candidate, tmp_path / "different-candidate.json")

    assert snapshot["payload"]["model_config"]["version"] == "synthetic-base"
    assert replay["payload"]["candidate_config"]["version"] == "different-candidate-version"
    assert replay["payload"]["candidate_id"] == candidate_id_for(candidate)
