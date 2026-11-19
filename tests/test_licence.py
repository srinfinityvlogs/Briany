# SPDX-License-Identifier: Apache-2.0
"""Every Python file carries the licence line (the licence itself is in LICENSE)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINE = "# SPDX-License-Identifier: Apache-2.0"


def test_every_python_file_names_its_licence():
    files = sorted([*ROOT.glob("briany/*.py"), *ROOT.glob("tests/*.py"), ROOT / "app.py", ROOT / "conftest.py"])
    assert len(files) > 40
    missing = [f.name for f in files if LINE not in f.read_text().splitlines()[:2]]
    assert missing == []
