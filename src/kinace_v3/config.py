from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.v3.json"
DEFAULT_DATABASE_PATH = Path(r"D:\kinacesqlv3.2\kinace_v3_2.sqlite3")
PROTECTED_DATABASE_ROOTS = (
    Path(r"D:\kinacesql"),
    Path(r"D:\kinacesql2"),
)
DEFAULT_V31_CLONE_DATABASE = Path(r"D:\kinacesqlv3.2\kinace_v3_1.sqlite3")
DATABASE_PATH_ENV = "KINACE_V32_DB_PATH"


class ConfigError(ValueError):
    """Raised when the V3 configuration is invalid."""


class ForbiddenDatabasePath(ConfigError):
    """Raised when a database would share a protected Kinace database tree."""


def _normalized_path(value: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(value)))
    path = Path(expanded)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((str(path), str(root)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def validate_database_path(value: str | os.PathLike[str]) -> Path:
    candidate = _normalized_path(value)
    for root in PROTECTED_DATABASE_ROOTS:
        forbidden = _normalized_path(root)
        if _is_within(candidate, forbidden):
            raise ForbiddenDatabasePath(
                f"database path is forbidden because it is under {forbidden}: {candidate}"
            )
    return candidate


def _database_value(config: Mapping[str, Any]) -> str:
    database = config.get("database")
    if not isinstance(database, Mapping):
        raise ConfigError("configuration must contain a database object")
    value = database.get("path")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("database.path must be a non-empty string")
    return value


def _apply_override(config: dict[str, Any]) -> dict[str, Any]:
    override = os.environ.get(DATABASE_PATH_ENV)
    if not override:
        return config
    result = copy.deepcopy(config)
    database = result.setdefault("database", {})
    if not isinstance(database, dict):
        raise ConfigError("configuration must contain a database object")
    database["path"] = override
    return result


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    environment_path = os.environ.get("KINACE_V32_CONFIG")
    config_path = _normalized_path(
        path or environment_path or DEFAULT_CONFIG_PATH
    )
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ConfigError("configuration root must be an object")
    config = _apply_override(config)
    validate_database_path(_database_value(config))
    return config


def database_path(config: Mapping[str, Any] | None = None) -> Path:
    if config is None:
        config = load_config()
    else:
        config = _apply_override(dict(config))
    return validate_database_path(_database_value(config))
