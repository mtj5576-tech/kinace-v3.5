from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sqlite3

import pytest


FORMAL_SURFACES = (
    "database",
    "snapshots",
    "trial_ledger",
    "reports",
    "holdout_registry",
)

SANDBOX_SURFACES = (
    "database",
    "snapshots",
    "trial_ledger",
    "outputs",
    "reports",
    "holdout_registry",
)


def _sandbox_module():
    try:
        return importlib.import_module("kinace_v3.synthetic_sandbox")
    except ModuleNotFoundError as exc:
        if exc.name == "kinace_v3.synthetic_sandbox":
            pytest.fail(
                "missing isolated synthetic-sandbox path resolver: "
                "kinace_v3.synthetic_sandbox"
            )
        raise


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _formal_fixture(tmp_path: Path) -> dict[str, object]:
    base = tmp_path / "non-formal"
    formal_root = base / "formal"
    snapshots = formal_root / "snapshots"
    reports = formal_root / "reports"
    snapshots.mkdir(parents=True)
    reports.mkdir(parents=True)
    _write_text(snapshots / "formal-snapshot.json", '{"track": "formal"}\n')
    _write_text(reports / "formal-report.md", "formal report\n")

    database = formal_root / "formal.sqlite3"
    trial_ledger = formal_root / "trial-ledger.jsonl"
    holdout_registry = formal_root / "holdout-registry.json"
    _write_text(database, "formal database sentinel\n")
    _write_text(trial_ledger, '{"trial_id": "formal-trial"}\n')
    _write_text(holdout_registry, '{"access_count": 16}\n')

    return {
        "base": base,
        "formal_root": formal_root,
        "formal_paths": {
            "database": database,
            "snapshots": snapshots,
            "trial_ledger": trial_ledger,
            "reports": reports,
            "holdout_registry": holdout_registry,
        },
    }


def _resolve(module, root: Path, fixture: dict[str, object]):
    return module.resolve_sandbox_paths(
        root=root,
        formal_paths=fixture["formal_paths"],
        protected_roots=(fixture["formal_root"],),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def test_resolver_owns_every_surface_under_one_isolated_root(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    sandbox_root = fixture["base"] / "sandbox"

    layout = _resolve(module, sandbox_root, fixture)

    assert set(SANDBOX_SURFACES) <= set(layout)
    for surface in SANDBOX_SURFACES:
        assert _is_within(layout[surface], sandbox_root)
        assert not _is_within(layout[surface], fixture["formal_root"])
    assert Path(layout["database"]).name == "research.sqlite3"
    assert Path(layout["trial_ledger"]).name == "trial-ledger.jsonl"
    assert Path(layout["holdout_registry"]).name == "holdout-access-registry.json"
    assert Path(layout["snapshots"]).name == "snapshots"
    assert Path(layout["outputs"]).name == "outputs"
    assert Path(layout["reports"]).name == "reports"


def test_sandbox_run_paths_cannot_target_formal_surfaces(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    sandbox_root = fixture["base"] / "sandbox"
    layout = _resolve(module, sandbox_root, fixture)

    for key in ("snapshots", "outputs", "reports"):
        Path(layout[key]).mkdir(parents=True, exist_ok=True)
    _write_text(Path(layout["database"]), "sandbox database\n")
    _write_text(Path(layout["trial_ledger"]), '{"trial_id": "sandbox-trial"}\n')
    _write_text(Path(layout["holdout_registry"]), '{"access_count": 0}\n')
    _write_text(Path(layout["snapshots"]) / "sandbox-snapshot.json", "{}\n")
    _write_text(Path(layout["outputs"]) / "sandbox-run.json", "{}\n")
    _write_text(Path(layout["reports"]) / "sandbox-report.md", "sandbox report\n")

    formal_paths = fixture["formal_paths"]
    assert Path(formal_paths["database"]).read_text(encoding="utf-8") == (
        "formal database sentinel\n"
    )
    assert "sandbox-trial" not in Path(formal_paths["trial_ledger"]).read_text(
        encoding="utf-8"
    )
    assert Path(formal_paths["holdout_registry"]).read_text(encoding="utf-8") == (
        '{"access_count": 16}\n'
    )
    assert not (Path(formal_paths["snapshots"]) / "sandbox-snapshot.json").exists()
    assert not (Path(formal_paths["reports"]) / "sandbox-report.md").exists()


@pytest.mark.parametrize("surface", FORMAL_SURFACES)
def test_resolver_rejects_equal_or_descendant_overlap_with_each_formal_surface(
    tmp_path, surface
):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    formal_path = Path(fixture["formal_paths"][surface])
    overlapping_root = formal_path

    with pytest.raises(module.SandboxPathError, match="(?i)overlap|contain|formal"):
        _resolve(module, overlapping_root, fixture)

    with pytest.raises(module.SandboxPathError, match="(?i)overlap|contain|formal"):
        _resolve(module, overlapping_root / "sandbox", fixture)


def test_resolver_rejects_sandbox_ancestor_of_formal_root(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)

    with pytest.raises(module.SandboxPathError, match="(?i)overlap|contain|formal"):
        _resolve(module, fixture["formal_root"].parent, fixture)


def test_resolver_rejects_case_insensitive_path_alias(tmp_path):
    if os.name != "nt":
        pytest.skip("case-insensitive path contract is specific to Windows")
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    alias_root = fixture["base"] / "FORMAL"

    with pytest.raises(module.SandboxPathError, match="(?i)overlap|contain|formal"):
        _resolve(module, alias_root / "sandbox", fixture)


def test_resolver_rejects_symlinked_formal_alias(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    alias_root = fixture["base"] / "formal-link"
    try:
        alias_root.symlink_to(fixture["formal_root"], target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(module.SandboxPathError, match="(?i)overlap|contain|formal"):
        _resolve(module, alias_root / "sandbox", fixture)


def test_resolver_rejects_symlinked_owned_surface(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    sandbox_root = fixture["base"] / "sandbox"
    sandbox_root.mkdir()
    try:
        (sandbox_root / "snapshots").symlink_to(
            fixture["formal_paths"]["snapshots"],
            target_is_directory=True,
        )
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(module.SandboxPathError, match="(?i)overlap|contain|formal"):
        _resolve(module, sandbox_root, fixture)


def test_resolver_rejects_overlap_with_protected_database_root(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    protected_root = fixture["base"] / "protected-databases"
    protected_root.mkdir()

    with pytest.raises(module.SandboxPathError, match="(?i)protected|overlap|contain"):
        module.resolve_sandbox_paths(
            root=protected_root / "sandbox",
            formal_paths=fixture["formal_paths"],
            protected_roots=(protected_root,),
        )


def test_initialize_sandbox_creates_owned_schema_and_run_manifest(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    sandbox_root = fixture["base"] / "sandbox"

    initialized = module.initialize_sandbox(
        root=sandbox_root,
        formal_paths=fixture["formal_paths"],
        protected_roots=(fixture["formal_root"],),
    )

    assert initialized["track"] == "sandbox"
    assert initialized["run_id_prefix"].startswith("sandbox-")
    with sqlite3.connect(str(sandbox_root / "research.sqlite3")) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"schema_metadata", "data_collection_schema_metadata", "daily_runs"} <= tables

    run_id = module.sandbox_run_id(
        sandbox_root,
        purpose="fixture",
        identity={"fixture": "sandbox"},
    )
    manifest_path = module.write_sandbox_run_manifest(
        root=sandbox_root,
        run_id=run_id,
        payload={"artifact_type": "kinace_v3_sandbox_fixture"},
        formal_paths=fixture["formal_paths"],
        protected_roots=(fixture["formal_root"],),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["track"] == "sandbox"
    assert manifest["run_id"] == run_id

    verification = module.verify_sandbox(
        root=sandbox_root,
        formal_paths=fixture["formal_paths"],
        protected_roots=(fixture["formal_root"],),
    )
    assert verification["status"] == "passed"
    assert verification["track"] == "sandbox"
    assert verification["run_count"] == 1


def test_write_sandbox_run_manifest_rejects_path_traversal(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    sandbox_root = fixture["base"] / "sandbox"
    module.initialize_sandbox(
        root=sandbox_root,
        formal_paths=fixture["formal_paths"],
        protected_roots=(fixture["formal_root"],),
    )

    unsafe_ids = (
        "sandbox-..",
        "sandbox-../escape",
        "sandbox-a/b",
        "sandbox-a\\b",
        "sandbox-a/../../formal",
    )
    for run_id in unsafe_ids:
        with pytest.raises(module.SandboxError, match="(?i)safe|segment|escape"):
            module.write_sandbox_run_manifest(
                root=sandbox_root,
                run_id=run_id,
                payload={"artifact_type": "kinace_v3_sandbox_fixture"},
                formal_paths=fixture["formal_paths"],
                protected_roots=(fixture["formal_root"],),
            )

    assert not (fixture["formal_root"] / "escape").exists()
    assert not (fixture["base"] / "escape").exists()


def test_initialize_sandbox_rejects_overlap_before_writing(tmp_path):
    module = _sandbox_module()
    fixture = _formal_fixture(tmp_path)
    formal_snapshot_root = Path(fixture["formal_paths"]["snapshots"])
    root = formal_snapshot_root / "sandbox"

    with pytest.raises(module.SandboxPathError):
        module.initialize_sandbox(
            root=root,
            formal_paths=fixture["formal_paths"],
            protected_roots=(fixture["formal_root"],),
        )

    assert not root.exists()
    assert json.loads((formal_snapshot_root / "formal-snapshot.json").read_text()) == {
        "track": "formal"
    }
