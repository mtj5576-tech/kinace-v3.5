import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _env(**updates):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update(updates)
    return env


def _run(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "kinace_v3", *args],
        cwd=ROOT,
        env=env or _env(),
        check=False,
        capture_output=True,
        text=True,
    )


def _write_csv(path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> Path:
    lines = ["ts_code,trade_date,open,high,low,close"]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_collection_help_lists_all_commands_and_gate_flags():
    result = _run("--help")

    assert result.returncode == 0, result.stderr
    for command in (
        "data-backfill",
        "data-fill-provisional",
        "data-revise",
        "data-quality",
        "data-status",
    ):
        assert command in result.stdout

    data_gate_help = _run("data-gate", "--help")
    assert "--mode {development,release}" in data_gate_help.stdout
    for command in ("search", "train"):
        command_help = _run(command, "--help")
        assert "--development" in command_help.stdout


def test_offline_csv_backfill_revision_provisional_and_status_workflow(tmp_path):
    database = tmp_path / "v3-data.sqlite3"
    initial_csv = _write_csv(
        tmp_path / "initial.csv",
        [("000001.SZ", "20240102", "10", "11", "9.5", "10.5")],
    )

    backfill = _run(
        "data-backfill",
        "--provider",
        "csv",
        "--dataset",
        "daily_bars",
        "--input",
        str(initial_csv),
        "--source",
        "csv-e2e",
        "--database",
        str(database),
        "--json",
    )
    assert backfill.returncode == 0, backfill.stderr
    backfill_payload = json.loads(backfill.stdout)
    assert backfill_payload["inserted_count"] == 1
    assert backfill_payload["status"] == "backfilled_reliable"
    assert backfill_payload["gate_eligible"] is True

    keys = _write_json(
        tmp_path / "provisional-keys.json",
        [{"ts_code": "000002.SZ", "trade_date": "2024-01-03"}],
    )
    provisional = _run(
        "data-fill-provisional",
        "--dataset",
        "daily_bars",
        "--keys",
        str(keys),
        "--baseline",
        "10",
        "--database",
        str(database),
        "--json",
    )
    assert provisional.returncode == 0, provisional.stderr
    provisional_payload = json.loads(provisional.stdout)
    assert provisional_payload["provisional"] is True
    assert provisional_payload["gate_eligible"] is False
    assert provisional_payload["release_eligible"] is False

    corrected_csv = _write_csv(
        tmp_path / "corrected.csv",
        [("000001.SZ", "20240102", "10", "12", "9.5", "11.5")],
    )
    revision = _run(
        "data-revise",
        "--dataset",
        "daily_bars",
        "--input",
        str(corrected_csv),
        "--source",
        "manual-correction",
        "--database",
        str(database),
        "--json",
    )
    assert revision.returncode == 0, revision.stderr
    revision_payload = json.loads(revision.stdout)
    assert revision_payload["inserted_count"] == 1
    assert revision_payload["superseded_count"] == 1

    real_replacement_csv = _write_csv(
        tmp_path / "real-replacement.csv",
        [("000002.SZ", "20240103", "9.8", "10.2", "9.7", "10.0")],
    )
    replacement = _run(
        "data-backfill",
        "--dataset",
        "daily_bars",
        "--input",
        str(real_replacement_csv),
        "--status",
        "observed",
        "--source",
        "provider-real",
        "--database",
        str(database),
        "--json",
        env=_env(TUSHARE_TOKEN="must-not-appear"),
    )
    assert replacement.returncode == 0, replacement.stderr
    replacement_payload = json.loads(replacement.stdout)
    assert replacement_payload["inserted_count"] == 1
    assert replacement_payload["superseded_count"] == 1
    assert "must-not-appear" not in replacement.stdout
    assert "must-not-appear" not in replacement.stderr

    status = _run(
        "data-status",
        "--database",
        str(database),
        "--json",
        env=_env(TUSHARE_TOKEN="must-not-appear"),
    )
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["revision_count"] == 4
    assert status_payload["current_record_count"] == 2
    assert status_payload["current_by_dataset"]["daily_bars"] == {
        "current_records": 2,
        "gate_eligible": 2,
        "provisional": 0,
    }
    assert "must-not-appear" not in status.stdout
    assert "must-not-appear" not in status.stderr

    quality = _run(
        "data-quality",
        "--dataset",
        "daily_bars",
        "--database",
        str(database),
        "--json",
    )
    assert quality.returncode == 0, quality.stderr
    quality_payload = json.loads(quality.stdout)
    assert quality_payload["record_count"] == 2
    assert quality_payload["duplicate_key_count"] == 0

    development = _run(
        "data-gate",
        "--mode",
        "development",
        "--database",
        str(database),
        "--output",
        str(tmp_path / "development.json"),
        "--json",
    )
    release = _run(
        "data-gate",
        "--mode",
        "release",
        "--database",
        str(database),
        "--output",
        str(tmp_path / "release.json"),
        "--json",
    )
    assert development.returncode == 0, development.stderr
    assert release.returncode == 0, release.stderr
    assert json.loads(development.stdout)["development_only"] is True
    assert json.loads(release.stdout)["production_eligible"] is False
