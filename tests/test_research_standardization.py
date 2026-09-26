from __future__ import annotations

from kinace_v3.data_collection.ingest import ingest_records
from kinace_v3.data_collection.models import RevisionStatus, SectorMembershipRecord
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.feature_snapshot import FEATURE_NAMES
from kinace_v3.research_standardization import (
    build_standardization_diagnostic,
    load_pit_industry_by_date,
)


def _row(code: str, index: int) -> dict:
    raw_inputs = {feature: 1.0 for feature in FEATURE_NAMES}
    raw_inputs["gap"] = float(index)
    return {
        "code": code,
        "raw_inputs": raw_inputs,
        "labels": {"forward_return": 0.1 + index * 0.1},
        "replay_ready": True,
    }


def _snapshot(decision_date: str) -> dict:
    codes = [f"00000{index}" for index in range(1, 7)]
    return {
        "decision_date": decision_date,
        "snapshot_id": f"snapshot-{decision_date}",
        "rows": [_row(code, index) for index, code in enumerate(codes)],
    }


def test_standardization_diagnostic_outputs_four_ic_methods():
    snapshots = [
        _snapshot("2026-01-02"),
        _snapshot("2026-01-05"),
        _snapshot("2026-01-06"),
        _snapshot("2026-01-07"),
    ]
    market_caps = {
        item["decision_date"]: {
            row["code"]: 1_000_000_000.0 * (index + 1)
            for index, row in enumerate(item["rows"])
        }
        for item in snapshots
    }
    industries = {
        "000001": "industry-a",
        "000002": "industry-a",
        "000003": "industry-a",
        "000004": "industry-b",
        "000005": "industry-b",
        "000006": "industry-b",
    }

    result = build_standardization_diagnostic(
        snapshots=snapshots,
        industry_by_code=industries,
        market_cap_by_date=market_caps,
        industry_source="fixture",
        industry_lookahead_safe=True,
        market_cap_source="fixture",
        min_rows_per_date=3,
        min_dates=3,
    )

    gap = next(item for item in result["factors"] if item["factor"] == "gap")
    assert gap["methods"]["raw"]["mean_ic"] == 1.0
    assert gap["methods"]["mad_winsorized"]["mean_ic"] == 1.0
    assert gap["methods"]["industry_neutralized"]["mean_ic"] > 0
    assert gap["methods"]["industry_marketcap_neutralized"]["mean_ic"] > 0
    assert gap["methods"]["industry_marketcap_neutralized"]["available_dates"] == 4
    assert gap["all_method_sign_consistency"] == 1.0
    assert gap["classification"] == "stable_positive"
    assert result["method_availability"]["industry_marketcap_neutralized"] >= 1
    assert result["data_coverage"] == {
        "replay_ready_rows": 24,
        "industry_covered_rows": 24,
        "industry_coverage_ratio": 1.0,
        "market_cap_covered_rows": 24,
        "market_cap_coverage_ratio": 1.0,
    }
    assert result["read_only"] is True
    assert result["production_changed"] is False


def test_load_pit_industry_by_date_uses_effective_membership_intervals(tmp_path):
    database = tmp_path / "standards.sqlite3"
    store = RevisionStore(database)
    ingest_records(
        store,
        "sector_membership",
        [
                SectorMembershipRecord(
                    index_code="801010.SI",
                    con_code="000001.SZ",
                    in_date="2024-01-01",
                    out_date="2025-01-01",
                    industry_name="industry-a",
                ),
            SectorMembershipRecord(
                index_code="801020.SI",
                con_code="000001.SZ",
                in_date="2025-01-01",
                industry_name="industry-b",
            ),
            SectorMembershipRecord(
                index_code="801030.SI",
                con_code="000002.SZ",
                in_date="2024-01-01",
                out_date="2025-01-01",
                industry_name="industry-c",
            ),
            SectorMembershipRecord(
                index_code="801040.SI",
                con_code="000003.SZ",
                in_date="2020-01-01",
                industry_name="industry-d",
            ),
            SectorMembershipRecord(
                index_code="801050.SI",
                con_code="000003.SZ",
                in_date="2021-01-01",
                out_date="2022-01-01",
                industry_name="industry-e",
            ),
        ],
        status=RevisionStatus.BACKFILLED_RELIABLE,
        source="fixture",
        bulk=True,
    )

    result = load_pit_industry_by_date(
        database,
        decision_dates=["2024-06-30", "2025-06-30"],
        codes=["000001.SZ", "000002.SZ", "000003.SZ"],
    )

    assert result["2024-06-30"] == {
        "000001": "industry-a",
        "000002": "industry-c",
        "000003": "industry-d",
    }
    assert result["2025-06-30"] == {
        "000001": "industry-b",
        "000003": "industry-d",
    }


def test_date_specific_missing_industry_disables_industry_methods():
    snapshots = [
        _snapshot("2026-01-02"),
        _snapshot("2026-01-05"),
        _snapshot("2026-01-06"),
    ]
    market_caps = {
        item["decision_date"]: {
            row["code"]: 1_000_000_000.0 * (index + 1)
            for index, row in enumerate(item["rows"])
        }
        for item in snapshots
    }

    result = build_standardization_diagnostic(
        snapshots=snapshots,
        industry_by_date={item["decision_date"]: {} for item in snapshots},
        market_cap_by_date=market_caps,
        industry_source="pit_membership",
        industry_lookahead_safe=True,
        market_cap_source="fixture",
        min_rows_per_date=3,
        min_dates=3,
    )

    assert result["method_availability"]["raw"] >= 1
    assert result["method_availability"]["industry_neutralized"] == 0
    assert result["method_availability"]["industry_marketcap_neutralized"] == 0
    assert result["data_coverage"]["industry_coverage_ratio"] == 0.0
    assert result["data_coverage"]["market_cap_coverage_ratio"] == 1.0
