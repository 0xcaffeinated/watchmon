#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Storefronts as first-class objects.

The load-bearing behaviour here is URL storage: the price database is
committed to a public repository, so no stored row may carry an origin. When a
second storefront was bolted on, the database layer stripped only the first
one's origin — which would have published the second's domain in every row.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, jsonstore, sources  # noqa: E402
from watchmon.history import PriceHistory  # noqa: E402
from watchmon.models import Listing  # noqa: E402

A = "https://store-a.example.com"
B = "https://store-b.example.com"


@pytest.fixture(autouse=True)
def _bases(monkeypatch):
    monkeypatch.setattr(config, "SITE_BASE", A)
    monkeypatch.setattr(config, "SITE_B_BASE", B)


# ------------------------------------------------------------- storage -----


def test_each_source_strips_its_own_origin():
    assert sources.store_url(f"{A}/x/p/itm1") == "a:/x/p/itm1"
    assert sources.store_url(f"{B}/watches/y/9/buy") == "b:/watches/y/9/buy"


def test_no_stored_url_contains_a_domain():
    """The whole point: a public repo must not learn where we shop."""
    for url in (f"{A}/x/p/itm1", f"{B}/watches/y/9/buy"):
        stored = sources.store_url(url)
        assert "http" not in stored
        assert "example.com" not in stored


def test_round_trip_restores_the_right_origin():
    for url in (f"{A}/x/p/itm1", f"{B}/watches/y/9/buy"):
        assert sources.restore_url(sources.store_url(url)) == url


def test_legacy_rows_without_a_key_belong_to_the_primary_source():
    """Rows written before there was a second storefront are bare paths."""
    assert sources.restore_url("/x/p/itm9") == f"{A}/x/p/itm9"


def test_an_unknown_origin_is_stored_unchanged_rather_than_mangled():
    assert sources.store_url("https://elsewhere.test/a") == "https://elsewhere.test/a"


def test_history_never_persists_an_origin(tmp_path):
    h = PriceHistory(tmp_path / "h.db")
    h.record(
        [
            Listing(pid="p1", url=f"{A}/x/p/itm1", title="A", price=100, brand="x"),
            Listing(pid="p2", url=f"{B}/watches/y/9/buy", title="B", price=200, brand="y"),
        ],
        now=1_700_000_000.0,
    )
    import sqlite3

    rows = sqlite3.connect(str(tmp_path / "h.db")).execute("SELECT url FROM products").fetchall()
    assert rows and all("http" not in r[0] for r in rows), rows
    # ...and a link can still be handed to the user.
    assert h.product("p2")["url"] == f"{B}/watches/y/9/buy"


# ------------------------------------------------------------- registry ----


def test_sources_are_disabled_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "SITE_B_BASE", "")
    assert [s.key for s in sources.enabled()] == ["a"]


def test_both_sources_enabled_when_configured():
    assert sorted(s.key for s in sources.enabled()) == ["a", "b"]


def test_kinds_differ():
    assert sources.by_key("a").kind == "browser"
    assert sources.by_key("b").kind == "json"


# ------------------------------------------- the JSON catalogue parser -----

STATE = """<script>window.__myx = {"results":{"products":[
  {"productId":1,"productName":"Model One","brand":"Acme","price":899,"mrp":1499,
   "landingPageUrl":"cat/acme/model-one/1/buy",
   "inventoryInfo":[{"available":true}]},
  {"productId":2,"productName":"Model Two","brand":"Acme","price":500,"mrp":900,
   "landingPageUrl":"cat/acme/model-two/2/buy",
   "inventoryInfo":[{"available":false}]}]}};</script>"""


def test_catalogue_json_is_extracted_and_mapped():
    state = jsonstore.extract_state(STATE)
    products = jsonstore.products_in(state)
    assert len(products) == 2
    listing = jsonstore.to_listing(products[0], B)
    assert listing.pid == "1"
    assert listing.price == 899
    assert listing.url == f"{B}/cat/acme/model-one/1/buy"
    # Brand is a separate field here; folding it into the title is what lets
    # the shared brand and rule matching work unchanged.
    assert listing.title.startswith("Acme ")


def test_stock_comes_from_variant_availability():
    products = jsonstore.products_in(jsonstore.extract_state(STATE))
    assert jsonstore.in_stock_of(products[0]) is True
    assert jsonstore.in_stock_of(products[1]) is False


def test_missing_state_is_handled():
    assert jsonstore.extract_state("<html>no state here</html>") is None
    assert jsonstore.extract_state("") is None


def test_attributes_render_as_the_spec_text_rules_expect():
    """Converting the dict means every require_spec/reject_spec works here
    without the rule knowing which storefront it came from."""
    text = jsonstore.specs_as_text({"Movement": "Automatic", "Add-Ons": "NA", "Display": "Analog"})
    assert "Movement\nAutomatic" in text
    assert "Add-Ons" not in text  # placeholder values dropped

    from watchmon import parsing

    assert parsing.extract_spec(text, r"(type of )?movement") == "Automatic"


def test_unusable_nodes_are_skipped():
    assert jsonstore.to_listing({"productName": "x"}, B) is None
    assert jsonstore.to_listing({"productId": 5, "price": "free"}, B) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
