import json
from pathlib import Path

from kinace_v3.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATABASE_PATH,
    database_path,
    load_config,
)


def test_load_config_uses_environment_override(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "database": {
                    "path": r"D:\kinacesqlv3.2\env-override.sqlite3"
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KINACE_V32_CONFIG", str(config_path))

    config = load_config()

    assert config["database"]["path"] == r"D:\kinacesqlv3.2\env-override.sqlite3"


ROOT = Path(__file__).resolve().parents[1]


def test_default_config_is_v3_only(monkeypatch):
    monkeypatch.delenv("KINACE_V32_DB_PATH", raising=False)
    monkeypatch.setenv("KINACE_V2_DB_PATH", r"D:\kinacesql\kinace_v2.sqlite3")
    monkeypatch.setenv("KINACE_DB_PATH", r"D:\kinacesql\generic.sqlite3")
    monkeypatch.setenv("KINACE_V2_ROOT", r"C:\unrelated\kinace-v2")

    config = load_config()

    assert DEFAULT_CONFIG_PATH == ROOT / "config" / "config.v3.json"
    assert DEFAULT_DATABASE_PATH == Path(r"D:\kinacesqlv3.2\kinace_v3_2.sqlite3")
    assert database_path(config) == Path(
        "D:/kinacev3.3/data/kinacesqlv3.2/kinace_v3_2.sqlite3"
    )


def test_only_explicit_v3_override_wins(monkeypatch, tmp_path):
    override = tmp_path / "override.sqlite3"
    monkeypatch.setenv("KINACE_V2_DB_PATH", r"D:\kinacesql\kinace_v2.sqlite3")
    monkeypatch.setenv("KINACE_V32_DB_PATH", str(override))

    assert database_path(load_config()) == override.resolve()
