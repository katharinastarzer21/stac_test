#!/usr/bin/env python3
"""
Functional correctness tests for the STAC API.

Run with:
  pytest scripts/test_stac_functional.py -v

Environment variables:
  STAC_URL          target API base (default: https://stac.eodc.eu/api/v1)
  E2E_ENV           env label (default: dev)
  STAC_WRITE_TOKEN  Bearer token for item write access (skip ingest test if unset)

Metrics are pushed to Pushgateway by scripts/conftest.py after the session ends.
"""

import os
import time
import uuid
import pytest
import requests
from urllib.parse import urlparse, parse_qs

STAC_URL         = os.environ.get("STAC_URL", "https://stac.eodc.eu/api/v1")
TIMEOUT          = 20
STAC_WRITE_TOKEN = os.environ.get("STAC_WRITE_TOKEN")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def collection_id():
    r = requests.get(f"{STAC_URL}/collections", timeout=TIMEOUT)
    r.raise_for_status()
    cols = r.json().get("collections", [])
    if not cols:
        pytest.skip("No collections available on this STAC endpoint")
    return cols[0]["id"]


@pytest.fixture(scope="module")
def known_item_id(collection_id):
    r = requests.get(
        f"{STAC_URL}/collections/{collection_id}/items",
        params={"limit": 1}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    features = r.json().get("features", [])
    if not features:
        pytest.skip(f"No items found in collection '{collection_id}'")
    return features[0]["id"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_next_token(body: dict) -> str | None:
    """Return the next-page token from a STAC paged response, or None."""
    for link in body.get("links", []):
        if link.get("rel") == "next":
            href = link.get("href", "")
            qs = parse_qs(urlparse(href).query)
            for key in ("token", "page", "offset", "next"):
                if key in qs:
                    return qs[key][0]
            # some implementations encode the whole next URL — return it raw
            return href
    # OGC API – Features style: context.next or numberReturned+offset
    ctx = body.get("context", {})
    if "next" in ctx:
        return ctx["next"]
    return None

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_collections_not_empty():
    """Collections endpoint returns at least one collection."""
    r = requests.get(f"{STAC_URL}/collections", timeout=TIMEOUT)
    assert r.status_code == 200, f"HTTP {r.status_code}"
    cols = r.json().get("collections", [])
    assert len(cols) > 0, "collections list is empty"


def test_known_item_exists(collection_id, known_item_id):
    """A known item can be fetched by ID from its collection."""
    r = requests.get(
        f"{STAC_URL}/collections/{collection_id}/items/{known_item_id}",
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    assert r.json().get("id") == known_item_id


def test_search_with_collection_filter(collection_id):
    """POST /search filtered by collection returns at least one result."""
    r = requests.post(
        f"{STAC_URL}/search",
        json={"collections": [collection_id], "limit": 5},
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"HTTP {r.status_code}"
    features = r.json().get("features", [])
    assert len(features) > 0, f"no features returned for collection '{collection_id}'"


def test_pagination_no_overlap(collection_id):
    """Two consecutive pages of items share no item IDs."""
    r1 = requests.get(
        f"{STAC_URL}/collections/{collection_id}/items",
        params={"limit": 5}, timeout=TIMEOUT,
    )
    assert r1.status_code == 200
    body1 = r1.json()
    ids1 = {f["id"] for f in body1.get("features", [])}

    if len(ids1) < 5:
        pytest.skip("Collection has fewer than 5 items — pagination not testable")

    token = _extract_next_token(body1)
    if token is None:
        pytest.skip("No next-page token found — single-page collection")

    # Try token as query param first; fall back to full next href
    if token.startswith("http"):
        r2 = requests.get(token, timeout=TIMEOUT)
    else:
        r2 = requests.get(
            f"{STAC_URL}/collections/{collection_id}/items",
            params={"limit": 5, "token": token}, timeout=TIMEOUT,
        )
    assert r2.status_code == 200
    ids2 = {f["id"] for f in r2.json().get("features", [])}

    overlap = ids1 & ids2
    assert not overlap, f"page 1 and page 2 share item IDs: {overlap}"


@pytest.mark.skipif(not STAC_WRITE_TOKEN, reason="STAC_WRITE_TOKEN not set")
def test_ingest_visible_delete(collection_id):
    """
    POST a test item, verify it appears in /search within 60 s, then DELETE it.
    Requires STAC_WRITE_TOKEN to be set.
    """
    test_item_id = f"test-e2e-{uuid.uuid4().hex[:12]}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {STAC_WRITE_TOKEN}",
    }
    item = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": test_item_id,
        "geometry": {
            "type": "Point",
            "coordinates": [0.0, 0.0],
        },
        "bbox": [0.0, 0.0, 0.0, 0.0],
        "properties": {
            "datetime": "2000-01-01T00:00:00Z",
        },
        "links": [],
        "assets": {},
        "collection": collection_id,
    }

    # POST the test item
    post_url = f"{STAC_URL}/collections/{collection_id}/items"
    r = requests.post(post_url, json=item, headers=headers, timeout=TIMEOUT)
    assert r.status_code in (200, 201), \
        f"POST item failed with HTTP {r.status_code}: {r.text[:200]}"

    try:
        # Poll /search until the item appears (up to 60 s)
        visible = False
        for _ in range(12):
            time.sleep(5)
            rs = requests.post(
                f"{STAC_URL}/search",
                json={"ids": [test_item_id], "collections": [collection_id]},
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
            if rs.status_code == 200 and any(
                f["id"] == test_item_id for f in rs.json().get("features", [])
            ):
                visible = True
                break
        assert visible, f"item '{test_item_id}' not visible in /search after 60 s"
    finally:
        # Always attempt cleanup
        requests.delete(
            f"{STAC_URL}/collections/{collection_id}/items/{test_item_id}",
            headers=headers, timeout=TIMEOUT,
        )


def test_asset_href_format(collection_id):
    """Asset hrefs in item responses use a recognised URL scheme."""
    r = requests.get(
        f"{STAC_URL}/collections/{collection_id}/items",
        params={"limit": 3}, timeout=TIMEOUT,
    )
    assert r.status_code == 200
    features = r.json().get("features", [])
    for feat in features:
        for key, asset in feat.get("assets", {}).items():
            href = asset.get("href", "")
            if href:
                assert href.startswith(("http://", "https://", "s3://")), \
                    f"item '{feat['id']}' asset '{key}' has unexpected href: {href!r}"
