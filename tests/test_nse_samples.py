import json

from briany import nse_samples as ns


def test_truncate_cuts_long_strings_and_lists():
    data = {"a": "x" * 500, "rows": list(range(40)), "nested": [{"k": "y" * 400}]}
    out = ns.truncate(data, max_str=50, max_items=5)
    assert out["a"].startswith("x" * 50) and "500 chars" in out["a"]
    assert len(out["rows"]) == 6 and "35 more" in out["rows"][-1]
    assert "400 chars" in out["nested"][0]["k"]
    assert json.dumps(out)
    assert ns.truncate("short") == "short" and ns.truncate(5) == 5


def test_describe_and_first_record():
    assert "list of 2" in ns.describe([{"b": 1, "a": 2}, {}])
    assert "['a', 'b']" in ns.describe([{"b": 1, "a": 2}])
    assert "list lengths: {'data': 3}" in ns.describe({"data": [1, 2, 3], "x": 1})
    assert ns.first_record([{"a": 1}]) == {"a": 1}
    assert ns.first_record({"x": 1, "data": [{"b": 2}]}) == {"b": 2}
    assert ns.first_record([]) is None and ns.first_record("text") is None


def test_fetch_never_raises(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    monkeypatch.setattr(ns.urllib.request, "urlopen", boom)
    res = ns.fetch("https://example.invalid/x")
    assert res["status"] == 403 and res["error"] == "HTTPError"

    def boom2(*a, **k):
        raise TimeoutError()
    monkeypatch.setattr(ns.urllib.request, "urlopen", boom2)
    assert ns.fetch("https://example.invalid/x")["error"] == "TimeoutError"


def test_sample_urls_are_well_formed():
    names = [n for n, _ in ns.SAMPLES]
    assert len(names) == len(set(names))
    assert all(" " not in p for _, p in ns.SAMPLES)
