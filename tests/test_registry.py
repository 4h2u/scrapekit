"""Registry contract tests: no network, no browser worker."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "sites.example.yaml"

REQUIRED = ("name", "url", "wait", "min_rows", "js")
FORBIDDEN = ("amazon.", "ebay.", "etsy.com", "tiktok.com", "linkedin.com", "upwork.com")


def load_sites():
    data = yaml.safe_load(REGISTRY.read_text())
    return data["sites"] if isinstance(data, dict) else data


def test_registry_has_sites():
    sites = load_sites()
    assert len(sites) >= 3
    assert len({s["name"] for s in sites}) == len(sites)


def test_every_site_has_required_fields():
    for site in load_sites():
        for field in REQUIRED:
            assert field in site, f"{site.get('name')} missing {field}"
        assert isinstance(site["js"], list) and site["js"], "js must be a non-empty list"
        assert site["wait"], "wait must be a selector"
        assert int(site["min_rows"]) >= 1


def test_no_login_or_brand_sites():
    for site in load_sites():
        url = site["url"].lower()
        for domain in FORBIDDEN:
            assert domain not in url, f"{site['name']} points at {domain}"
