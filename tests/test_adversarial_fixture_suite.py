from __future__ import annotations

import json
from pathlib import Path

from kinace_v3.adversarial_suite import (
    REQUIRED_FIXTURE_IDS,
    AdversarialSuiteError,
    run_adversarial_suite,
)
from kinace_v3.cli import main as cli_main
from kinace_v3.synthetic_sandbox import MANIFEST_FILENAME


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "adversarial_execution_framework.json"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _formal_fixture(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "formal"
    snapshots = root / "snapshots"
    reports = root / "reports"
    snapshots.mkdir(parents=True)
    reports.mkdir(parents=True)
    _write(snapshots / "snapshot.json", '{"track":"formal"}\n')
    _write(reports / "report.md", "formal report\n")
    paths = {
        "database": root / "formal.sqlite3",
        "snapshots": snapshots,
        "trial_ledger": root / "trial-ledger.jsonl",
        "reports": reports,
        "holdout_registry": root / "holdout-registry.json",
    }
    _write(Path(paths["database"]), "formal database sentinel\n")
    _write(Path(paths["trial_ledger"]), '{"trial_id":"formal-trial"}\n')
    _write(Path(paths["holdout_registry"]), '{"access_count":0}\n')
    return {"root": root, "paths": paths}


def _run(tmp_path: Path, *, seed: int = 20260926):
    fixture = _formal_fixture(tmp_path)
    sandbox = tmp_path / "sandbox"
    output = tmp_path / "results.json"
    result = run_adversarial_suite(
        root=sandbox,
        fixtures_path=FIXTURE_PATH,
        output_path=output,
        seed=seed,
        formal_paths=fixture["paths"],
        protected_roots=(fixture["root"],),
    )
    return fixture, sandbox, output, result


def test_fixture_catalog_contains_all_required_injections():
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_ids = [fixture["id"] for fixture in payload["fixtures"]]

    assert fixture_ids == list(REQUIRED_FIXTURE_IDS)
    assert len(fixture_ids) == 12
    assert all(fixture["expected_rejection_reason"] for fixture in payload["fixtures"])

    case_reasons = {
        reason
        for fixture in payload["fixtures"]
        for reason in fixture["case_rejection_reasons"]
    }
    assert {
        "market_bars_out_of_order",
        "market_bars_duplicate",
        "market_bars_non_finite",
        "ohlc_inconsistent",
        "ohlc_status_missing",
        "unregistered_trial",
        "trial_budget_exceeded",
    } <= case_reasons


def test_all_fixtures_are_rejected_with_expected_reasons_inside_sandbox(tmp_path):
    fixture, sandbox, output, result = _run(tmp_path)

    assert result["status"] == "passed"
    assert result["track"] == "sandbox"
    assert result["injected_count"] == 12
    assert result["rejected_count"] == 12
    assert result["false_negative_count"] == 0
    assert result["case_false_negative_count"] == 0
    assert result["production_ready"] is False
    assert result["production_changed"] is False
    assert result["real_money_orders_enabled"] is False
    assert result["sandbox"]["verification"]["status"] == "passed"
    assert result["sandbox"]["verification"]["track"] == "sandbox"
    assert output.is_file()
    observed_case_reasons = {
        case["observed_rejection_reason"]
        for item in result["fixtures"]
        for case in item["cases"]
    }
    assert {
        "market_bars_out_of_order",
        "market_bars_duplicate",
        "market_bars_non_finite",
        "ohlc_inconsistent",
        "ohlc_status_missing",
        "unregistered_trial",
        "trial_budget_exceeded",
    } <= observed_case_reasons

    for item in result["fixtures"]:
        assert item["rejected"] is True
        assert item["status"] == "rejected"
        assert item["actual_rejection_reason"] == item["expected_rejection_reason"]
        assert all(case["matched"] for case in item["cases"])
        assert Path(item["sandbox_manifest_path"]).is_file()
        manifest = json.loads(
            Path(item["sandbox_manifest_path"]).read_text(encoding="utf-8")
        )
        assert manifest["track"] == "sandbox"
        assert manifest["run_id"].startswith("sandbox-")

    assert (sandbox / MANIFEST_FILENAME).is_file()
    assert Path(fixture["paths"]["database"]).read_text(encoding="utf-8") == (
        "formal database sentinel\n"
    )
    assert Path(fixture["paths"]["holdout_registry"]).read_text(encoding="utf-8") == (
        '{"access_count":0}\n'
    )


def test_same_seed_and_fixture_set_are_byte_identical(tmp_path):
    fixture = _formal_fixture(tmp_path)
    sandbox = tmp_path / "sandbox"
    output = tmp_path / "results.json"
    parameters = {
        "root": sandbox,
        "fixtures_path": FIXTURE_PATH,
        "output_path": output,
        "seed": 314159,
        "formal_paths": fixture["paths"],
        "protected_roots": (fixture["root"],),
    }

    first = run_adversarial_suite(**parameters)
    first_bytes = output.read_bytes()
    second = run_adversarial_suite(**parameters)

    assert first["result_sha256"] == second["result_sha256"]
    assert output.read_bytes() == first_bytes
    assert [
        (item["fixture_id"], item["actual_rejection_reason"])
        for item in first["fixtures"]
    ] == [
        (item["fixture_id"], item["actual_rejection_reason"])
        for item in second["fixtures"]
    ]


def test_wrong_expected_reason_is_counted_as_false_negative(tmp_path):
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["fixtures"][0]["expected_rejection_reason"] = "mutated_guard_expectation"
    mutated = tmp_path / "mutated-fixtures.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    fixture = _formal_fixture(tmp_path)

    result = run_adversarial_suite(
        root=tmp_path / "sandbox",
        fixtures_path=mutated,
        output_path=tmp_path / "mutated-results.json",
        formal_paths=fixture["paths"],
        protected_roots=(fixture["root"],),
    )

    assert result["status"] == "failed"
    assert result["injected_count"] == 12
    assert result["rejected_count"] == 11
    assert result["false_negative_count"] == 1
    assert result["fixtures"][0]["status"] == "false_negative"


def test_fixture_catalog_rejects_a_missing_required_injection(tmp_path):
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["fixtures"] = payload["fixtures"][:-1]
    incomplete = tmp_path / "incomplete-fixtures.json"
    incomplete.write_text(json.dumps(payload), encoding="utf-8")
    fixture = _formal_fixture(tmp_path)

    try:
        run_adversarial_suite(
            root=tmp_path / "sandbox",
            fixtures_path=incomplete,
            output_path=tmp_path / "results.json",
            formal_paths=fixture["paths"],
            protected_roots=(fixture["root"],),
        )
    except AdversarialSuiteError as exc:
        assert "missing required injections" in str(exc)
    else:  # pragma: no cover - fail closed assertion
        raise AssertionError("incomplete adversarial fixture set was accepted")


def test_cli_sandbox_adversarial_runs_deterministically(tmp_path, capsys):
    fixture = _formal_fixture(tmp_path)
    output = tmp_path / "cli-results.json"
    common = [
        "sandbox-adversarial",
        "--root",
        str(tmp_path / "sandbox"),
        "--fixtures",
        str(FIXTURE_PATH),
        "--output",
        str(output),
        "--formal-database",
        str(fixture["paths"]["database"]),
        "--formal-snapshots",
        str(fixture["paths"]["snapshots"]),
        "--formal-trial-ledger",
        str(fixture["paths"]["trial_ledger"]),
        "--formal-reports",
        str(fixture["paths"]["reports"]),
        "--formal-holdout-registry",
        str(fixture["paths"]["holdout_registry"]),
        "--protected-database-root",
        str(fixture["root"]),
        "--json",
    ]

    assert cli_main(common) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "passed"
    assert first["injected_count"] == first["rejected_count"] == 12
    assert first["false_negative_count"] == 0

    assert cli_main(common) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["result_sha256"] == first["result_sha256"]
