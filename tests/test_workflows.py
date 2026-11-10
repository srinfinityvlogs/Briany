"""Guards against workflow mistakes that GitHub only reveals as 'Workflow dispatch failed'.

YAML 1.1 reads bare on/off/yes/no/true/false as booleans, so an input named `on` silently
becomes `true` and the workflow cannot be dispatched.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.y*ml"))
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_dispatch_inputs_are_real_names_and_every_reference_exists(path):
    text = path.read_text()
    doc = yaml.safe_load(text)
    triggers = doc.get(True, doc.get("on")) or {}                 # PyYAML reads the top-level `on:` as True too
    dispatch = triggers.get("workflow_dispatch") if isinstance(triggers, dict) else None
    inputs = (dispatch.get("inputs") or {}) if isinstance(dispatch, dict) else {}
    for name in inputs:
        assert isinstance(name, str) and NAME.fullmatch(name), (
            f"{path.name}: input {name!r} is not a valid name (a bare on/off/yes/no/true/false key becomes a boolean)")
    referenced = set(re.findall(r"github\.event\.inputs\.([A-Za-z_][A-Za-z0-9_-]*)", text))
    assert referenced <= set(inputs), f"{path.name}: refers to undefined inputs {sorted(referenced - set(inputs))}"
