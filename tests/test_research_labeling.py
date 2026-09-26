from __future__ import annotations

import json

import pytest

from kinace_v3.research_labeling import (
    build_forward_labels,
    label_value,
    run_label_alignment_audit,
    summarize_holding_periods,
)


def _rows() -> list[dict]:
    return [
        {"trade_date": "2026-01-01", "open": 99.0, "close": 100.0},
        {"trade_date": "2026-01-02", "open": 100.0, "close": 101.0},
        {"trade_date": "2026-01-03", "open": 101.0, "close": 102.0},
        {"trade_date": "2026-01-06", "open": 102.0, "close": 103.0},
        {"trade_date": "2026-01-07", "open": 103.0, "close": 104.0},
        {"trade_date": "2026-01-08", "open": 104.0, "close": 105.0},
    ]


def test_multi_horizon_labels_use_next_open_entry():
    labels = build_forward_labels(_rows(), 0)

    assert labels["forward_return"] == pytest.approx(0.01)
    assert labels["forward_return_3d"] == pytest.approx(0.03)
    assert labels["forward_return_5d"] == pytest.approx(0.05)
    assert labels["primary_label"] == "forward_return_5d"
    assert labels["primary_return"] == pytest.approx(0.05)
    assert labels["entry"] == {
        "date": "2026-01-02",
        "price": 100.0,
        "timing": "next_trading_day_open",
    }
    assert label_value(labels, 5) == pytest.approx(0.05)


def test_holding_period_summary_reports_exit_mix():
    result = summarize_holding_periods(
        [
            {"hold_sessions": 2, "exit_reason": "stop"},
            {"hold_sessions": 5, "exit_reason": "time_stop"},
            {"hold_sessions": 7, "exit_reason": "target"},
            {"hold_sessions": 5, "exit_reason": "end_of_backtest"},
        ]
    )

    assert result["trade_count"] == 4
    assert result["average_hold_sessions"] == pytest.approx(4.75)
    assert result["median_hold_sessions"] == pytest.approx(5.0)
    assert result["atr_exit_count"] == 2
    assert result["time_exit_count"] == 1
    assert result["forced_end_count"] == 1


def test_label_alignment_audit_reports_primary_coverage(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "2026-01-01.json").write_text(
        json.dumps(
            {
                "artifact_type": "kinace_v3_feature_snapshot",
                "decision_date": "2026-01-01",
                "rows": [
                    {
                        "code": "000001",
                        "replay_ready": True,
                        "labels": {
                            "forward_return": 0.01,
                            "forward_return_3d": 0.02,
                            "forward_return_5d": 0.03,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_label_alignment_audit(
        tmp_path / "labels.sqlite3",
        snapshot_dir=snapshot_dir,
        output_path=tmp_path / "alignment.json",
        markdown_path=tmp_path / "alignment.md",
    )

    assert result["status"] == "completed"
    assert result["primary_label_horizon"] == 5
    assert result["label_coverage"]["label_coverage_ratio"]["5"] == 1.0
