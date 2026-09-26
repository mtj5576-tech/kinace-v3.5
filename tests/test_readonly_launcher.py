from __future__ import annotations

import sys

from kinace_v3.portal_readonly import DEFAULT_READONLY_PORT
from kinace_v3.readonly_launcher import (
    application_root,
    build_parser,
    latest_readonly_database,
)


def test_readonly_launcher_defaults_to_dedicated_port():
    args = build_parser().parse_args([])

    assert args.port == DEFAULT_READONLY_PORT
    assert args.host == "127.0.0.1"
    assert args.no_browser is False


def test_frozen_launcher_finds_project_root_from_supplement_folder(tmp_path, monkeypatch):
    project = tmp_path / "project"
    executable = project / "release" / "0920-portal-update" / "portal.exe"
    (project / "src" / "kinace_v3").mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    monkeypatch.delenv("KINACE_V32_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert application_root() == project.resolve()


def test_latest_readonly_database_prefers_reconstruction_copy(tmp_path):
    database = tmp_path / "outputs" / "reconstruction" / "kinace_v3_2.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"sqlite")

    assert latest_readonly_database(tmp_path) == database.resolve()
