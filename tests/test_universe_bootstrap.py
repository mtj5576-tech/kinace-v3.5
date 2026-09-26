from __future__ import annotations

from kinace_v3.data_collection.models import SectorMembershipRecord
from kinace_v3.data_collection.store import RevisionStore
from kinace_v3.universe_bootstrap import run_universe_bootstrap


class _IndustryProvider:
    source_name = "fixture"

    def index_classify(self, **_: object) -> list[dict[str, str]]:
        return [
            {
                "index_code": "801010.SI",
                "industry_name": "industry-a",
                "level": "L1",
                "src": "SW2021",
            }
        ]

    def industry_member(self, index_code: str, **kwargs: object) -> list[SectorMembershipRecord]:
        assert "is_new" not in kwargs
        return [
            SectorMembershipRecord(
                index_code=index_code,
                con_code="000001.SZ",
                in_date="2024-01-01",
                out_date=None,
                industry_name="industry-a",
                is_new="Y",
            ),
            SectorMembershipRecord(
                index_code=index_code,
                con_code="000002.SZ",
                in_date="2020-01-01",
                out_date="2021-01-01",
                industry_name="industry-a",
                is_new="N",
            ),
        ]


def test_universe_bootstrap_ingests_open_industry_members(tmp_path):
    database = tmp_path / "universe.sqlite3"

    result = run_universe_bootstrap(
        database,
        index_codes=[],
        list_statuses=[],
        provider=_IndustryProvider(),
    )

    records = RevisionStore(database).current_records("sector_membership")
    assert result["results"]["industry_membership"][0]["inserted_count"] == 2
    assert any(row["record"]["out_date"] is None for row in records)
    assert any(row["record"]["is_new"] == "Y" for row in records)
