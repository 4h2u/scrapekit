"""Normalizer and dedupe tests against the real engine functions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrapekit.engine import dedupe, normalize  # noqa: E402


def test_normalize_collapses_whitespace_and_prices():
    rows = normalize([{"title": "  A   Light\tin the Attic ", "price": "£51.77"}])
    assert rows[0]["title"] == "A Light in the Attic"
    assert rows[0]["price"] == "51.77"


def test_normalize_relative_dates():
    rows = normalize([{"age": "15 hours ago"}, {"age": "3 days ago"}])
    assert rows[0]["age"] == "15h"
    assert rows[1]["age"] == "3d"


def test_dedupe_keeps_first_occurrence():
    rows = [{"title": "a"}, {"title": "A"}, {"title": "b"}]
    out = dedupe(rows, "title")
    assert [r["title"] for r in out] == ["a", "b"]


def test_parse_amount_formats():
    from scrapekit.engine import parse_amount

    assert parse_amount("£51.77") == 51.77
    assert parse_amount("140,08 €") == 140.08
    assert parse_amount("1.234,56") == 1234.56
    assert parse_amount("1,234.56") == 1234.56
    assert parse_amount("12,5") == 12.5
    assert parse_amount("no price here") is None


def test_parse_amount_does_not_mistake_decimals_for_thousands():
    from scrapekit.engine import parse_amount

    assert parse_amount("0.001") == 0.001
    assert parse_amount("51.777") == 51.777
    assert parse_amount("1.000") == 1000.0


def test_unreachable_worker_returns_unreachable_state(monkeypatch):
    import scrapekit.engine as engine

    monkeypatch.setattr(engine, "SOCK", "/tmp/definitely-not-a-socket")
    result = engine.run_site({"name": "x", "url": "https://example.com", "wait": "body",
                              "min_rows": 1, "js": ["() => []"]})
    assert result["state"] == "unreachable"
    assert "unreachable" in result["source"]
