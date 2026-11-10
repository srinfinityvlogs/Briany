from datetime import datetime, timedelta

import pytest

from briany.nsefiles import FetchError
from briany.probe_publication import IST, MAX_CONSECUTIVE_ERRORS, main, probe


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def run(responses, start=(19, 30), until=(22, 30), interval=120):
    """responses: list consumed per fetch; items are text, None, or an exception."""
    clock, lines, script = Clock(datetime(2026, 11, 10, *start, tzinfo=IST)), [], list(responses)

    def fetch(day):
        x = script.pop(0) if script else None
        if isinstance(x, Exception):
            raise x
        return x
    found = probe("2026-11-10", fetch=fetch, now=clock.now, sleep=clock.sleep,
                  until=datetime(2026, 11, 10, *until, tzinfo=IST), interval=interval, log=lines.append)
    return found, lines, clock


def test_reports_the_window_in_which_the_file_appeared():
    found, lines, _ = run([None, None, None, "csv-text"])
    assert found == datetime(2026, 11, 10, 19, 36, tzinfo=IST)                      # fourth poll, 3 x 120 s later
    text = "\n".join(lines)
    assert "FILE AVAILABLE" in text and "first seen" in text
    assert "published between 19:34:00 IST and 19:36:00 IST" in text and "no file yet" in text


def test_file_already_there_is_only_an_upper_bound():
    found, lines, _ = run(["csv-text"])
    assert found == datetime(2026, 11, 10, 19, 30, tzinfo=IST)
    assert "already available at the first check" in "\n".join(lines) and "UPPER bound" in "\n".join(lines)


def test_gives_up_at_the_cutoff_without_claiming_anything():
    found, lines, clock = run([], start=(22, 20), until=(22, 30), interval=300)
    assert found is None and "cut-off reached" in lines[-1]
    assert clock.t >= datetime(2026, 11, 10, 22, 30, tzinfo=IST)


def test_transient_errors_are_tolerated_but_a_block_is_not():
    found, lines, _ = run([FetchError("HTTP 500"), FetchError("timeout"), "csv-text"])
    assert found is not None and sum("fetch problem" in l for l in lines) == 2
    with pytest.raises(FetchError):
        run([FetchError("HTTP 403")] * MAX_CONSECUTIVE_ERRORS)
    ok, _, _ = run([FetchError("x"), "csv-text", FetchError("y")])                  # a success resets the count
    assert ok is not None


def test_only_on_guard_ignores_other_dates(capsys):
    main(["--only-on", "2001-01-01"])
    assert "nothing to do" in capsys.readouterr().out


def test_bad_arguments_are_rejected():
    for bad in (["--date", "10-11-2026"], ["--until", "25:99"], ["--until", "7pm"]):
        with pytest.raises(SystemExit):
            main(bad)
