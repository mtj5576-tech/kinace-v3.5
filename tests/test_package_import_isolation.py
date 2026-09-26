import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_importing_v3_does_not_load_v2():
    script = "\n".join(
        [
            "import sys",
            "import kinace_v3",
            "v2_modules = sorted(",
            "    name for name in sys.modules",
            "    if name == 'kinace_v2' or name.startswith('kinace_v2.')",
            ")",
            "assert not v2_modules, v2_modules",
            "print(kinace_v3.__file__)",
        ]
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    imported = Path(result.stdout.strip()).resolve()
    assert imported.is_relative_to((ROOT / "src" / "kinace_v3").resolve())
