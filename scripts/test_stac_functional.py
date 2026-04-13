import os
import time
import pytest
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse, parse_qs

STAC_URL    = os.environ.get("STAC_URL", "https://stac.eodc.eu/api/v1")
TIMEOUT     = 20
INGEST_URL  = os.environ.get("INGEST_URL")
INGEST_USER = os.environ.get("INGEST_USER")
INGEST_PASS = os.environ.get("INGEST_PASSWORD")

_INGEST_COLLECTION = "SENTINEL1_GRD"
_INGEST_ITEM_ID    = "monitoring"
_INGEST_ITEM = {
    "type": "Feature",
    "stac_version": "1.0.0",
    "id": _INGEST_ITEM_ID,
    "properties": {"datetime": "2023-07-20T00:00:00Z"},
    "geometry": {
        "type": "Polygon",
        "coordinates": [[
            [31.80093,  77.341131], [31.964415, 77.391375],
            [32.368193, 77.512526], [32.78097,  77.633047],
            [33.202279, 77.752939], [33.631543, 77.872266],
            [34.069244, 77.990875], [34.514775, 78.10891 ],
            [34.969084, 78.226219], [35.072215, 78.252211],
            [36.245595, 78.224194], [35.545752, 77.252124],
            [31.80093,  77.341131],
        ]],
    },
    "links": [],
    "assets": {},
    "bbox": [31.80093, 77.252124, 36.245595, 78.252211],
    "stac_extensions": [],
    "collection": _INGEST_COLLECTION,
}

@pytest.fixture(scope="module")
def collection_id():
    override = os.environ.get("STAC_FUNCTIONAL_COLLECTION")
    if override:
        return override
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

# Tests

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


@pytest.mark.skipif(
    not (INGEST_URL and INGEST_USER and INGEST_PASS),
    reason="INGEST_URL / INGEST_USER / INGEST_PASSWORD not set",
)
def test_ingest_visible_delete():
    """
    POST a test item via the ingest API (Basic auth), verify it appears in
    /search within 60 s, then DELETE it via the ingest API.
    Uses fixed collection SENTINEL1_GRD and item id 'monitoring'.
    """
    auth       = HTTPBasicAuth(INGEST_USER, INGEST_PASS)
    base       = INGEST_URL.rstrip("/")
    post_url   = f"{base}/collections/{_INGEST_COLLECTION}/items"
    delete_url = f"{base}/collections/{_INGEST_COLLECTION}/items/{_INGEST_ITEM_ID}"

    # POST — on 409 conflict the item already exists; delete it and retry once
    r = requests.post(post_url, json=_INGEST_ITEM, auth=auth, timeout=TIMEOUT)
    if r.status_code == 409:
        requests.delete(delete_url, auth=auth, timeout=TIMEOUT)
        r = requests.post(post_url, json=_INGEST_ITEM, auth=auth, timeout=TIMEOUT)
    assert r.status_code in (200, 201), \
        f"POST failed: HTTP {r.status_code}: {r.text[:200]}"

    try:
        # Poll STAC read API until the item appears (up to 60 s)
        visible = False
        for _ in range(12):
            time.sleep(5)
            rs = requests.post(
                f"{STAC_URL}/search",
                json={"ids": [_INGEST_ITEM_ID], "collections": [_INGEST_COLLECTION]},
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
            if rs.status_code == 200 and any(
                f["id"] == _INGEST_ITEM_ID for f in rs.json().get("features", [])
            ):
                visible = True
                break
        assert visible, f"item '{_INGEST_ITEM_ID}' not visible in /search after 60 s"
    finally:
        requests.delete(delete_url, auth=auth, timeout=TIMEOUT)


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
