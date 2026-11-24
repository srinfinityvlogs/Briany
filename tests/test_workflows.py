# SPDX-License-Identifier: Apache-2.0
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


def _crons(doc):
    triggers = doc.get(True, doc.get("on")) or {}
    sched = triggers.get("schedule") if isinstance(triggers, dict) else None
    return [e["cron"] for e in (sched or [])]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_schedule_starts_on_the_hour(path):
    """GitHub names the start of every hour its busiest time for scheduled runs; they are delayed or dropped there. The
    ingest-flows crons were all at minute 0 and no scheduled run was seen, and NSE keeps only the latest day, so a dropped
    run loses a day for good."""
    for cron in _crons(yaml.safe_load(path.read_text())):
        assert cron.split()[0] != "0", f"{path.name}: {cron!r} starts on the hour; pick another minute"


def test_the_daily_flow_snapshot_has_several_chances_and_is_also_part_of_the_price_run():
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    flows = yaml.safe_load((root / "ingest-flows.yml").read_text())
    assert len(_crons(flows)) >= 4
    prices = yaml.safe_load((root / "ingest.yml").read_text())
    steps = prices["jobs"]["ingest"]["steps"]
    extra = [s for s in steps if "flows_ingest" in str(s.get("run", ""))]
    assert len(extra) == 1 and extra[0].get("continue-on-error") is True and "schedule" in str(extra[0].get("if"))
    assert "REDIS_URL" in (extra[0].get("env") or {})
