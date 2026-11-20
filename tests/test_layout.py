# SPDX-License-Identifier: Apache-2.0
"""Guards for the Vercel layout: what gets installed and shipped, and that the runtime needs nothing else."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGES = {"fastapi", "requests", "redis", "tzdata"}
PHASE1_ONLY = {"pandas", "yfinance", "feedparser"}


def names(path):
    out = set()
    for line in (ROOT / path).read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(re.split(r"[<>=!~\[ ;]", line, maxsplit=1)[0].lower())
    return out


def test_vercel_installs_only_runtime_packages():
    """requirements.txt is what Vercel installs: the runtime packages and their own dependencies, nothing from Phase 1."""
    found = names("requirements.txt")
    assert RUNTIME_PACKAGES <= found, f"missing {sorted(RUNTIME_PACKAGES - found)}"
    assert not (found & PHASE1_ONLY)


def test_every_line_of_requirements_txt_is_an_exact_pin():
    """A new release of a library must never change a deployment by itself; see 'Updating dependencies' in the README."""
    lines = [l.split("#")[0].strip() for l in (ROOT / "requirements.txt").read_text().splitlines()]
    lines = [l for l in lines if l]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9_.+!\-]*", l) for l in lines), \
        [l for l in lines if "==" not in l]


def test_the_direct_runtime_dependencies_are_listed_in_requirements_in():
    """requirements.in is the list a person edits; the pin-dependencies workflow turns it into exact versions."""
    assert names("requirements.in") == RUNTIME_PACKAGES


def test_every_workflow_install_file_exists():
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        for target in re.findall(r"pip install -r (\S+)", wf.read_text()):
            assert (ROOT / target).is_file(), f"{wf.name} installs {target}, which does not exist"


def test_retired_files_stay_retired():
    """The hosting spike, the Phase 1 feasibility scripts, the Northflank Dockerfile and the one-off publication probe.
    An empty spike/ folder (or one holding only __pycache__) can linger on a machine after `git rm`; git does not track it."""
    gone = ["Dockerfile", "phase1.yaml", "phase1_checks.py", "llm_suite.py", "requirements-phase1.txt",
            ".github/workflows/phase1.yml", "briany/probe_publication.py", "tests/test_probe.py"]
    assert [g for g in gone if (ROOT / g).exists()] == []
    spike = ROOT / "spike"
    left = [p for p in spike.rglob("*") if p.is_file() and "__pycache__" not in p.parts] if spike.exists() else []
    assert left == []


def test_dev_requirements_cover_the_web_tests():
    assert {"pytest", "redis", "fakeredis", "pyyaml", "fastapi", "httpx"} <= names("requirements-dev.txt")


def test_vercel_json_pins_mumbai_and_the_entry_point_exists():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    assert cfg["regions"] == ["bom1"]
    assert re.search(r"^app = FastAPI\(", (ROOT / "app.py").read_text(), re.M)


def test_vercelignore_keeps_tests_and_ci_files_out_of_the_bundle():
    lines = {l.strip() for l in (ROOT / ".vercelignore").read_text().splitlines()}
    assert {"tests/", ".github/"} <= lines
    assert "briany/" not in lines and "requirements.txt" not in lines and "app.py" not in lines


def test_the_web_app_imports_with_only_the_runtime_packages():
    """Block everything Vercel would not have installed, then import the whole runtime path."""
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        f"        if name.split('.')[0] in {sorted(PHASE1_ONLY | {'yaml', 'pytest', 'fakeredis', 'httpx'})!r}:\n"
        "            raise ImportError('blocked: ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import app, briany.runtime, briany.telegram_bot, briany.ingest, briany.nse, briany.news, briany.llm, briany.store\n"
    )
    pytest.importorskip("fastapi")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
