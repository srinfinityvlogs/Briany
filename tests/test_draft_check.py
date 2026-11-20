# SPDX-License-Identifier: Apache-2.0
import pytest

from briany.answer import validate_summary
from briany.draft_check import category, evaluate, fixture_packets, pick, report
from briany.evidence import human_date
from briany.llm import LlmError

PACKETS = fixture_packets()


def good_text(packet):
    close, chg = packet.facts[0], next(f for f in packet.facts if f.label.startswith("Change from previous close"))
    return f"{packet.name} closed at {close.value} on {human_date(close.date)}. The change from the previous close was {chg.value}."


class Writer:
    def __init__(self, name, script):
        self.name, self.script, self.calls = name, script, []

    def write(self, system, user, max_tokens):
        self.calls.append((system, user, max_tokens))
        step = self.script(len(self.calls), user)
        if isinstance(step, Exception):
            raise step
        return step


def packet_for(user):
    return next(p for _id, p in PACKETS if p.name in user and p.facts[0].value in user)


def test_the_recorded_why_scenarios_are_the_packets_that_get_tried():
    assert len(PACKETS) == 18 and all(p.kind == "move" and p.status == "ok" for _i, p in PACKETS)   # 6 stocks x 3 days
    assert PACKETS[0][0].startswith("why:")


def test_pick_spreads_the_choice_over_the_whole_list():
    ids = [i for i, _p in PACKETS]
    chosen = [i for i, _p in pick(PACKETS, 6)]
    assert len(chosen) == 6 and len({i.split(":")[1] for i in chosen}) >= 4          # several stocks, not one stock's three days
    assert chosen == sorted(chosen, key=ids.index)
    assert pick(PACKETS, 0) == PACKETS and pick(PACKETS, 99) == PACKETS


def test_a_good_draft_is_accepted_and_a_bad_one_is_rejected_with_the_reason():
    ok = Writer("fine", lambda n, user: good_text(packet_for(user)))
    bad = Writer("sloppy", lambda n, user: "Reliance rose 202 percent after strong demand.")
    rows_ok, rows_bad = (evaluate(w, pick(PACKETS, 3), pause=0, sleep=lambda s: None) for w in (ok, bad))
    assert all(r["problems"] == [] and r["error"] is None for r in rows_ok) and len(ok.calls) == 3
    assert all(any("202" in p for p in r["problems"]) for r in rows_bad)
    system, user, max_tokens = ok.calls[0]
    assert "opening summary" in system and user.startswith("Packet:") and max_tokens == 900
    assert validate_summary(good_text(PACKETS[0][1]), PACKETS[0][1]) == []                  # the helper's text really is valid


def test_a_rate_limit_waits_a_minute_and_tries_again_once():
    sleeps = []
    w = Writer("groq", lambda n, user: LlmError("rate_limit") if n == 1 else good_text(packet_for(user)))
    rows = evaluate(w, pick(PACKETS, 1), pause=0, sleep=sleeps.append)
    assert rows[0]["error"] is None and rows[0]["problems"] == [] and sleeps == [60] and len(w.calls) == 2
    stuck = Writer("groq", lambda n, user: LlmError("rate_limit"))
    row = evaluate(stuck, pick(PACKETS, 1), pause=0, sleep=lambda s: None)[0]
    assert row["error"] == "rate_limit" and row["draft"] is None and len(stuck.calls) == 2


def test_other_provider_errors_are_recorded_not_raised():
    w = Writer("gemini", lambda n, user: LlmError("server", "HTTP 503"))
    rows = evaluate(w, pick(PACKETS, 2), pause=0, sleep=lambda s: None)
    assert [r["error"] for r in rows] == ["server", "server"] and len(w.calls) == 2


def test_pauses_go_between_calls_not_after_the_last():
    sleeps = []
    evaluate(Writer("g", lambda n, user: good_text(packet_for(user))), pick(PACKETS, 3), pause=7, sleep=sleeps.append)
    assert sleeps == [7, 7]


def test_categories_group_problems_without_their_numbers_or_words():
    assert category("number not in the data: 202") == category("number not in the data: 3.5%") == "number not in the data"
    assert category("summary too long (4 lines, 600 characters)") == "summary too long (N lines, N characters)"
    assert category("missing key figure: the official close") == "missing key figure"


def test_the_report_shows_rejected_drafts_in_full_with_counts_and_errors():
    bad = Writer("groq", lambda n, user: "Reliance rose 202 percent after strong demand.")
    ok = Writer("gemini", lambda n, user: good_text(packet_for(user)))
    down = Writer("other", lambda n, user: LlmError("auth"))
    packets = pick(PACKETS, 2)
    text = report({w.name: evaluate(w, packets, pause=0, sleep=lambda s: None) for w in (bad, ok, down)})
    assert "===== groq: 0 accepted, 2 rejected, 0 errors of 2 packets" in text
    assert "number not in the data x2" in text and "draft: 'Reliance rose 202 percent after strong demand.'" in text
    assert "===== gemini: 2 accepted, 0 rejected" in text and "ACCEPTED (example)" in text
    assert "===== other: 0 accepted, 0 rejected, 2 errors" in text and "(auth)" in text


def test_no_key_means_a_clear_exit(monkeypatch):
    from briany import draft_check
    for var in ("GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("KV_REDIS_URL", raising=False)
    with pytest.raises(SystemExit, match="no model key"):
        draft_check.main(["--max", "1"])
