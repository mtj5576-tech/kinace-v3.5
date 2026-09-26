from __future__ import annotations

from kinace_v3.data_collection import (
    RevisionStore,
    audit_records,
    audit_store,
    evaluate_quality_eligibility,
    fill_provisional,
    select_decision_window,
)


def _bar(trade_date: str = "2024-01-02", close: float = 10.5) -> dict:
    return {
        "ts_code": "000001.SZ",
        "trade_date": trade_date,
        "open": 10.0,
        "high": max(11.0, close),
        "low": 9.8,
        "close": close,
        "pre_close": 9.9,
        "vol": 1000.0,
        "amount": 10000.0,
        "is_suspended": False,
        "is_limit_up": False,
        "is_limit_down": False,
        "labels": {"forward_return": 0.01},
        "decision_date": trade_date,
        "observed_at_utc": f"{trade_date}T10:00:00Z",
    }


def test_quality_report_calculates_coverage_state_labels_and_pit():
    report = audit_records(
        "daily_bars",
        [_bar()],
        as_of_utc="2024-01-02T12:00:00Z",
        require_state_inputs=True,
        require_point_in_time=True,
    )

    assert report["passed"] is True
    assert report["required_field_coverage"]["close"]["ratio"] == 1.0
    assert report["duplicate_keys"]["duplicate_count"] == 0
    assert report["invalid_ohlc_relationships"]["valid"] is True
    assert report["suspended_limit_state_inputs"]["state_inputs_ready"] is True
    assert report["label_readiness"]["ready"] is True
    assert report["point_in_time_eligibility"]["eligible"] is True


def test_quality_report_fails_missing_duplicate_and_invalid_values():
    invalid = _bar(close=12.0)
    invalid["high"] = 11.0
    invalid["is_limit_up"] = "not-a-boolean"
    missing = _bar()
    missing.pop("close")

    report = audit_records(
        "daily_bars",
        [invalid, missing],
        require_state_inputs=True,
        require_point_in_time=True,
    )

    codes = {issue["code"] for issue in report["issues"]}
    assert report["status"] == "failed"
    assert "missing_required_fields" in codes
    assert "duplicate_keys" in codes
    assert "invalid_ohlc_relationships" in codes
    assert "invalid_suspended_limit_state_inputs" in codes
    assert report["duplicate_keys"]["duplicate_count"] == 1


def test_quality_checks_date_order_label_readiness_and_future_information():
    bad_dates = audit_records(
        "stock_metadata",
        [
            {
                "ts_code": "000001.SZ",
                "name": "PAYH",
                "list_date": "2024-02-01",
                "delist_date": "2024-01-01",
            }
        ],
    )
    assert bad_dates["date_ordering"]["valid"] is False

    future = audit_records(
        "daily_bars",
        [_bar()],
        as_of_utc="2024-01-01T12:00:00Z",
        require_point_in_time=True,
    )
    assert future["point_in_time_eligibility"]["future_information_count"] == 1
    assert future["point_in_time_eligibility"]["eligible"] is False

    no_labels = audit_records(
        "daily_bars",
        [{key: value for key, value in _bar().items() if key != "labels"}],
    )
    assert no_labels["label_readiness"]["ready"] is False


def test_development_window_is_eligible_but_provisional_release_is_not(tmp_path):
    store = RevisionStore(tmp_path / "quality.sqlite3")
    fill_provisional(
        store,
        "daily_bars",
        [{"ts_code": "000001.SZ", "trade_date": "2024-01-02"}],
        baseline=10.0,
    )
    revision = store.current_revision(
        "daily_bars", {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}
    )
    report = audit_records(
        "daily_bars",
        [
            {
                "record": revision["record"],
                "effective_status": revision["effective_status"],
                "is_suspended": True,
                "is_limit_up": False,
                "is_limit_down": False,
                "labels": {"forward_return": 0.01},
                "decision_date": "2024-01-02",
                "observed_at_utc": "2024-01-02T10:00:00Z",
            }
        ],
        require_state_inputs=True,
        require_point_in_time=True,
    )

    development = evaluate_quality_eligibility(
        report,
        mode="development",
        max_decision_dates=20,
    )
    release = evaluate_quality_eligibility(report, mode="release")

    assert development["development_eligible"] is True
    assert development["development_only"] is True
    assert development["production_eligible"] is False
    assert release["release_eligible"] is False
    assert "provisional_records" in release["release_blockers"]
    assert audit_store(store, "daily_bars")["point_in_time_eligibility"][
        "provisional_count"
    ] == 1


def test_shortened_decision_window_selects_latest_dates():
    records = [
        _bar("2024-01-02"),
        _bar("2024-01-03"),
        _bar("2024-01-04"),
    ]
    selected = select_decision_window(records, max_decision_dates=2)
    assert [row["trade_date"] for row in selected] == [
        "2024-01-03",
        "2024-01-04",
    ]
