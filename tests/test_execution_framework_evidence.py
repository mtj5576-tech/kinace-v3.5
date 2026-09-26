from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from kinace_v3.cli import main as cli_main


HASH_FIELDS = (
    "source_tree_sha256",
    "config_sha256",
    "database_sha256",
    "snapshot_set_sha256",
    "evaluation_protocol_sha256",
    "holdout_vault_sha256",
)

REQUIRED_MANIFEST_FIELDS = {
    "code_commit",
    "config_path",
    "database_path",
    "evaluation_protocol_path",
    "holdout_access_count",
    "holdout_vault_path",
    "production_changed",
    "production_ready",
    "snapshot_dir",
    *HASH_FIELDS,
}


def _formal_evidence_module():
    try:
        return importlib.import_module("kinace_v3.formal_evidence")
    except ModuleNotFoundError as exc:
        if exc.name == "kinace_v3.formal_evidence":
            pytest.fail(
                "missing formal-evidence manifest/read-only validator: "
                "kinace_v3.formal_evidence"
            )
        raise


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "non-formal" / "formal"
    source_root = root / "source"
    snapshots = root / "snapshots"
    source_root.mkdir(parents=True)
    (source_root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    snapshots.mkdir(parents=True)
    _write_json(snapshots / "2026-01-02.json", {"decision_date": "2026-01-02"})

    config = root / "config.json"
    protocol = root / "evaluation-protocol.json"
    holdout_vault = root / "holdout-vault.json"
    _write_json(config, {"config_version": "test-v1"})
    _write_json(protocol, {"protocol_version": "test-v1", "binding": True})
    _write_json(
        holdout_vault,
        {"window": {"start": "2026-02-01", "end": "2026-03-31"}},
    )

    database = root / "research.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence_rows (row_id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO evidence_rows DEFAULT VALUES")

    return {
        "root": root,
        "source_root": source_root,
        "snapshots": snapshots,
        "config": config,
        "protocol": protocol,
        "holdout_vault": holdout_vault,
        "database": database,
    }


def _build_manifest(tmp_path: Path):
    module = _formal_evidence_module()
    fixture = _fixture(tmp_path)
    manifest = module.build_formal_evidence_manifest(
        source_root=fixture["source_root"],
        code_commit="a" * 40,
        config_path=fixture["config"],
        database_path=fixture["database"],
        snapshot_dir=fixture["snapshots"],
        evaluation_protocol_path=fixture["protocol"],
        holdout_vault_path=fixture["holdout_vault"],
        holdout_access_count=0,
        production_ready=False,
        production_changed=False,
    )
    manifest_path = tmp_path / "non-formal" / "evidence" / "manifest.json"
    module.write_formal_evidence_manifest(manifest, manifest_path)
    module.manifest_anchor_path(manifest_path).chmod(stat.S_IREAD)
    return module, fixture, manifest, manifest_path


def _artifact_paths(fixture: dict[str, Path]) -> list[Path]:
    paths = [
        fixture["source_root"],
        fixture["snapshots"],
        fixture["config"],
        fixture["protocol"],
        fixture["holdout_vault"],
        fixture["database"],
    ]
    paths.extend(fixture["source_root"].rglob("*"))
    paths.extend(fixture["snapshots"].rglob("*"))
    return paths


def _set_read_only(path: Path, read_only: bool) -> None:
    if not path.exists():
        return
    if path.is_dir():
        children = sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for child in children:
            _set_read_only(child, read_only)
        path.chmod(0o500 if read_only else 0o700)
    else:
        path.chmod(0o400 if read_only else 0o600)


def _tree_state(paths: list[Path]) -> dict[str, tuple[str, int, bytes | None]]:
    state: dict[str, tuple[str, int, bytes | None]] = {}
    for path in paths:
        if path.is_dir():
            state[str(path)] = ("dir", 0, None)
        elif path.is_file():
            data = path.read_bytes()
            state[str(path)] = ("file", len(data), data)
    return state


def _expected_raw_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_binds_required_hashes_and_production_flags(tmp_path):
    _module, fixture, manifest, _manifest_path = _build_manifest(tmp_path)

    assert REQUIRED_MANIFEST_FIELDS <= set(manifest)
    assert manifest["code_commit"] == "a" * 40
    assert Path(manifest["config_path"]).resolve() == fixture["config"].resolve()
    assert Path(manifest["database_path"]).resolve() == fixture["database"].resolve()
    assert Path(manifest["snapshot_dir"]).resolve() == fixture["snapshots"].resolve()
    assert (
        Path(manifest["evaluation_protocol_path"]).resolve()
        == fixture["protocol"].resolve()
    )
    assert Path(manifest["holdout_vault_path"]).resolve() == fixture["holdout_vault"].resolve()
    assert manifest["config_sha256"] == _expected_raw_hash(fixture["config"])
    assert manifest["database_sha256"] == _expected_raw_hash(fixture["database"])
    assert manifest["evaluation_protocol_sha256"] == _expected_raw_hash(fixture["protocol"])
    assert manifest["holdout_vault_sha256"] == _expected_raw_hash(fixture["holdout_vault"])
    for field in HASH_FIELDS:
        assert len(manifest[field]) == 64
        int(manifest[field], 16)
    assert manifest["holdout_access_count"] == 0
    assert manifest["production_ready"] is False
    assert manifest["production_changed"] is False


def test_validator_is_read_only_and_rejects_writable_formal_artifacts(tmp_path):
    module, fixture, _manifest, manifest_path = _build_manifest(tmp_path)
    paths = [
        *_artifact_paths(fixture),
        manifest_path,
        module.manifest_anchor_path(manifest_path),
    ]
    try:
        for path in paths:
            _set_read_only(path, True)
        before = _tree_state(paths)

        report = module.verify_formal_evidence_manifest(manifest_path)

        assert report["status"] == "passed"
        assert report["read_only"] is True
        assert report["production_ready"] is False
        assert report["production_changed"] is False
        assert _tree_state(paths) == before

        fixture["config"].chmod(stat.S_IREAD | stat.S_IWRITE)
        with pytest.raises(module.FormalEvidenceError, match="(?i)writable|read-only"):
            module.verify_formal_evidence_manifest(manifest_path)
    finally:
        ordered = sorted(paths, key=lambda item: len(item.parts), reverse=True)
        for path in ordered:
            _set_read_only(path, False)


def test_validator_detects_bound_artifact_tampering(tmp_path):
    module, fixture, _manifest, manifest_path = _build_manifest(tmp_path)
    _write_json(
        fixture["protocol"],
        {"protocol_version": "tampered", "binding": True},
    )

    with pytest.raises(module.FormalEvidenceError, match="(?i)hash|changed|tamper"):
        module.verify_formal_evidence_manifest(manifest_path)


def test_validator_fails_closed_when_bound_artifact_is_missing(tmp_path):
    module, fixture, _manifest, manifest_path = _build_manifest(tmp_path)
    fixture["database"].unlink()

    with pytest.raises(module.FormalEvidenceError, match="(?i)missing|not found"):
        module.verify_formal_evidence_manifest(manifest_path)


def test_validator_detects_rehashed_manifest_metadata_tampering(tmp_path):
    module, _fixture_paths, _manifest, manifest_path = _build_manifest(tmp_path)
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["production_ready"] = True
    tampered["holdout_access_count"] = 1
    tampered[module.MANIFEST_HASH_FIELD] = module._manifest_digest(tampered)
    manifest_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    _write_json(manifest_path, tampered)

    with pytest.raises(module.FormalEvidenceError, match="(?i)anchor|changed"):
        module.verify_formal_evidence_manifest(manifest_path)


def test_manifest_rejects_overlapping_formal_artifacts(tmp_path):
    module = _formal_evidence_module()
    fixture = _fixture(tmp_path)
    overlapping_config = fixture["snapshots"] / "config.json"
    _write_json(overlapping_config, {"config_version": "overlap"})

    with pytest.raises(module.FormalEvidenceError, match="(?i)overlap|contain"):
        module.build_formal_evidence_manifest(
            source_root=fixture["source_root"],
            code_commit="b" * 40,
            config_path=overlapping_config,
            database_path=fixture["database"],
            snapshot_dir=fixture["snapshots"],
            evaluation_protocol_path=fixture["protocol"],
            holdout_vault_path=fixture["holdout_vault"],
            holdout_access_count=0,
            production_ready=False,
            production_changed=False,
        )


def test_cli_freeze_and_verify_are_non_mutating(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    manifest_path = tmp_path / "non-formal" / "evidence" / "manifest.json"
    common = [
        "--source-root",
        str(fixture["source_root"]),
        "--code-commit",
        "c" * 40,
        "--database",
        str(fixture["database"]),
        "--snapshot-dir",
        str(fixture["snapshots"]),
        "--evaluation-protocol",
        str(fixture["protocol"]),
        "--holdout-vault",
        str(fixture["holdout_vault"]),
    ]

    assert (
        cli_main(
            [
                "--config",
                str(fixture["config"]),
                "evidence-freeze",
                *common,
                "--output",
                str(manifest_path),
                "--json",
            ]
        )
        == 0
    )
    frozen = json.loads(capsys.readouterr().out)
    assert frozen["status"] == "frozen"
    assert Path(frozen["manifest_path"]) == manifest_path.resolve()

    paths = [
        *_artifact_paths(fixture),
        manifest_path,
        importlib.import_module("kinace_v3.formal_evidence").manifest_anchor_path(
            manifest_path
        ),
    ]
    try:
        for path in paths:
            _set_read_only(path, True)
        before = _tree_state(paths)

        assert cli_main(["evidence-verify", "--manifest", str(manifest_path), "--json"]) == 0
        verified = json.loads(capsys.readouterr().out)
        assert verified["status"] == "passed"
        assert verified["read_only"] is True
        assert verified["production_ready"] is False
        assert verified["production_changed"] is False
        assert _tree_state(paths) == before
    finally:
        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            _set_read_only(path, False)
