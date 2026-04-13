import os, time
import pytest
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse, parse_qs

STAC_URL   = os.environ.get("STAC_URL", "https://stac.eodc.eu/api/v1")
TIMEOUT    = 20
INGEST_URL = os.environ.get("INGEST_URL")
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
        pytest.skip("No collections available")
    return cols[0]["id"]


@pytest.fixture(scope="module")
def known_item_id(collection_id):
    r = requests.get(f"{STAC_URL}/collections/{collection_id}/items", params={"limit": 1}, timeout=TIMEOUT)
    r.raise_for_status()
    features = r.json().get("features", [])
    if not features:
        pytest.skip(f"No items in collection '{collection_id}'")
    return features[0]["id"]


def test_collections_not_empty():
    r = requests.get(f"{STAC_URL}/collections", timeout=TIMEOUT)
    assert r.status_code == 200, f"HTTP {r.status_code}"
    assert len(r.json().get("collections", [])) > 0


def test_known_item_exists(collection_id, known_item_id):
    r = requests.get(f"{STAC_URL}/collections/{collection_id}/items/{known_item_id}", timeout=TIMEOUT)
    assert r.status_code == 200, f"HTTP {r.status_code}"
    assert r.json().get("id") == known_item_id


def test_search_with_collection_filter(collection_id):
    r = requests.post(f"{STAC_URL}/search",
                      json={"collections": [collection_id], "limit": 5},
                      timeout=TIMEOUT)
    assert r.status_code == 200, f"HTTP {r.status_code}"
    assert len(r.json().get("features", [])) > 0


def test_pagination_no_overlap(collection_id):
    r1 = requests.get(f"{STAC_URL}/collections/{collection_id}/items", params={"limit": 5}, timeout=TIMEOUT)
    assert r1.status_code == 200
    ids1 = {f["id"] for f in r1.json().get("features", [])}
    if len(ids1) < 5:
        pytest.skip("Collection has fewer than 5 items")

    token = None
    for link in r1.json().get("links", []):
        if link.get("rel") == "next":
            href = link.get("href", "")
            qs = parse_qs(urlparse(href).query)
            token = next((qs[k][0] for k in ("token", "page", "offset", "next") if k in qs), href)
            break
    if not token:
        pytest.skip("No next-page token — single-page collection")

    r2 = requests.get(token if token.startswith("http") else
                      f"{STAC_URL}/collections/{collection_id}/items",
                      params={"limit": 5, "token": token} if not token.startswith("http") else {},
                      timeout=TIMEOUT)
    assert r2.status_code == 200
    ids2 = {f["id"] for f in r2.json().get("features", [])}
    assert not ids1 & ids2, f"Pages overlap: {ids1 & ids2}"


@pytest.mark.skipif(
    not (INGEST_URL and INGEST_USER and INGEST_PASS),
    reason="INGEST_URL / INGEST_USER / INGEST_PASSWORD not set",
)
def test_ingest_visible_delete():
    auth       = HTTPBasicAuth(INGEST_USER, INGEST_PASS)
    post_url   = f"{INGEST_URL.rstrip('/')}/collections/{_INGEST_COLLECTION}/items"
    delete_url = f"{INGEST_URL.rstrip('/')}/collections/{_INGEST_COLLECTION}/items/{_INGEST_ITEM_ID}"

    r = requests.post(post_url, json=_INGEST_ITEM, auth=auth, timeout=TIMEOUT)
    if r.status_code == 409:
        requests.delete(delete_url, auth=auth, timeout=TIMEOUT)
        r = requests.post(post_url, json=_INGEST_ITEM, auth=auth, timeout=TIMEOUT)
    assert r.status_code in (200, 201), f"POST failed: HTTP {r.status_code}: {r.text[:200]}"

    try:
        visible = False
        for _ in range(12):
            time.sleep(5)
            rs = requests.post(f"{STAC_URL}/search",
                               json={"ids": [_INGEST_ITEM_ID], "collections": [_INGEST_COLLECTION]},
                               timeout=TIMEOUT)
            if rs.status_code == 200 and any(f["id"] == _INGEST_ITEM_ID for f in rs.json().get("features", [])):
                visible = True
                break
        assert visible, f"Item '{_INGEST_ITEM_ID}' not visible in /search after 60s"
    finally:
        requests.delete(delete_url, auth=auth, timeout=TIMEOUT)


def test_asset_href_format(collection_id):
    r = requests.get(f"{STAC_URL}/collections/{collection_id}/items", params={"limit": 3}, timeout=TIMEOUT)
    assert r.status_code == 200
    for feat in r.json().get("features", []):
        for key, asset in feat.get("assets", {}).items():
            href = asset.get("href", "")
            if href:
                assert href.startswith(("http://", "https://", "s3://")), \
                    f"Item '{feat['id']}' asset '{key}' has unexpected href: {href!r}"
