from pathlib import Path

import pytest

from kinace_v3.config import ForbiddenDatabasePath, validate_database_path


@pytest.mark.parametrize(
    "path",
    [
        r"D:\kinacesql\kinace_v3_2.sqlite3",
        r"d:\KINACESQL\nested\kinace_v3_2.sqlite3",
        r"D:\kinacesql\..\kinacesql\kinace_v3_2.sqlite3",
        r"D:\kinacesql",
        r"D:\kinacesql2\kinace_v3_1.sqlite3",
        r"d:\KINACESQL2\nested\kinace_v3_2.sqlite3",
    ],
)
def test_forbidden_tree_is_rejected(path):
    with pytest.raises(ForbiddenDatabasePath):
        validate_database_path(path)


def test_dedicated_sibling_tree_is_allowed():
    path = validate_database_path(r"D:\kinacesqlv3.2\kinace_v3_2.sqlite3")

    assert path == Path(r"D:\kinacev3.3\data\kinacesqlv3.2\kinace_v3_2.sqlite3")
