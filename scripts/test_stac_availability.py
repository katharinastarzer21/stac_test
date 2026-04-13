#!/usr/bin/env python3
"""
STAC API availability prober.
Runs a fixed set of probes against STAC_URL and pushes per-probe metrics
to Pushgateway, then a roll-up via e2e_helpers.prom.push_e2e_result.

Environment variables:
  STAC_URL               target API base (default: https://stac.eodc.eu/api/v1)
  E2E_ENV                label injected into every metric (default: dev)
  PUSHGATEWAY_URL        base URL of the Pushgateway (optional; skip push if unset)
  PUSHGATEWAY_USERNAME   basic-auth username
  PUSHGATEWAY_PASSWORD   basic-auth password
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from e2e_helpers.prom import push_e2e_result
from prometheus_client import CollectorRegistry, Gauge, generate_latest


STAC_URL    = os.environ.get("STAC_URL", "https://stac.eodc.eu/api/v1")
TIMEOUT     = int(os.environ.get("STAC_TIMEOUT", "20"))
LOG_FILE    = "results/logs/test_stac_availability.log"
SERVICE     = "stac"
ENV         = os.environ.get("E2E_ENV", "dev")
PUSHGW_URL  = os.environ.get("PUSHGATEWAY_URL")
PUSHGW_USER = os.environ.get("PUSHGATEWAY_USERNAME")
PUSHGW_PASS = os.environ.get("PUSHGATEWAY_PASSWORD")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def push_probe(probe_name: str, collection: str, success: bool,
               duration: float, http_status: int):
    if not PUSHGW_URL:
        log.debug("PUSHGATEWAY_URL not set — skipping push for %s", probe_name)
        return

    reg = CollectorRegistry()
    Gauge("eodc_e2e_probe_success",
          "1 success 0 failure", registry=reg).set(1 if success else 0)
    Gauge("eodc_e2e_probe_duration_seconds",
          "probe duration in seconds", registry=reg).set(float(duration))
    Gauge("eodc_e2e_probe_http_status",
          "last HTTP status code (0 = network error)", registry=reg).set(float(http_status))
    Gauge("eodc_e2e_probe_last_run_timestamp",
          "unix timestamp of last probe run", registry=reg).set(time.time())

    grouping_key = {"env": ENV, "service": SERVICE, "probe": probe_name}
    if collection:
        grouping_key["collection"] = collection

    base = PUSHGW_URL.rstrip("/")
    path = "/".join(f"{k}/{v}" for k, v in sorted(grouping_key.items()))
    url  = f"{base}/metrics/job/e2e_direct/{path}"
    body = generate_latest(reg)
    auth = (PUSHGW_USER, PUSHGW_PASS) if PUSHGW_USER and PUSHGW_PASS else None

    try:
        resp = requests.put(
            url, data=body, auth=auth, timeout=15,
            headers={"Content-Type": "text/plain; version=0.0.4"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        log.info("pushed  probe=%-20s  collection=%-30s  success=%s  dur=%.3fs",
                 probe_name, collection or "(global)", success, duration)
    except Exception as e:
        log.warning("push_probe failed  probe=%s  collection=%s  error=%s",
                    probe_name, collection, e)


def _timed_get(url, **kwargs):
    t0 = time.perf_counter()
    try:
        r = requests.get(url, timeout=TIMEOUT, **kwargs)
        return r, time.perf_counter() - t0, r.status_code, None
    except Exception as e:
        return None, time.perf_counter() - t0, 0, str(e)

def _timed_post(url, **kwargs):
    t0 = time.perf_counter()
    try:
        r = requests.post(url, timeout=TIMEOUT, **kwargs)
        return r, time.perf_counter() - t0, r.status_code, None
    except Exception as e:
        return None, time.perf_counter() - t0, 0, str(e)

def _timed_head(url, **kwargs):
    t0 = time.perf_counter()
    try:
        r = requests.head(url, timeout=TIMEOUT, allow_redirects=True, **kwargs)
        return r, time.perf_counter() - t0, r.status_code, None
    except Exception as e:
        return None, time.perf_counter() - t0, 0, str(e)

def _validate_response(resp, expect_json=True):
    code = resp.status_code
    if not (200 <= code < 300):
        return False, f"HTTP {code}", code
    if expect_json:
        ct = resp.headers.get("Content-Type", "").lower()
        if "application/json" not in ct and "+json" not in ct:
            return False, f"bad content-type: {ct}", code
        try:
            resp.json()
        except Exception as e:
            return False, f"invalid JSON: {e}", code
    return True, "OK", code

ProbeResult = dict  # keys: name, collection, success, duration, http_status, msg, extras

def probe_root() -> ProbeResult:
    resp, dur, code, err = _timed_get(f"{STAC_URL}/")
    if err:
        return dict(name="root", collection="", success=False, duration=dur,
                    http_status=0, msg=f"network error: {err}", extras={})
    ok, reason, _ = _validate_response(resp)
    return dict(name="root", collection="", success=ok, duration=dur,
                http_status=code, msg=reason, extras={})

def probe_collections() -> ProbeResult:
    resp, dur, code, err = _timed_get(f"{STAC_URL}/collections")
    if err:
        return dict(name="collections_list", collection="", success=False, duration=dur,
                    http_status=0, msg=f"network error: {err}", extras={})
    ok, reason, _ = _validate_response(resp)
    extras = {}
    if ok:
        try:
            cols = resp.json().get("collections", [])
            extras["collection_count"] = len(cols)
            extras["collection_ids"] = [
                c.get("id") for c in cols if isinstance(c, dict) and c.get("id")
            ]
        except Exception:
            pass
    return dict(name="collections_list", collection="", success=ok, duration=dur,
                http_status=code, msg=reason, extras=extras)

def probe_collection_detail(col_id: str) -> ProbeResult:
    resp, dur, code, err = _timed_get(f"{STAC_URL}/collections/{col_id}")
    if err:
        return dict(name="collection_detail", collection=col_id, success=False,
                    duration=dur, http_status=0, msg=f"network error: {err}", extras={})
    ok, reason, _ = _validate_response(resp)
    return dict(name="collection_detail", collection=col_id, success=ok,
                duration=dur, http_status=code, msg=reason, extras={})

def probe_items(col_id: str) -> ProbeResult:
    resp, dur, code, err = _timed_get(
        f"{STAC_URL}/collections/{col_id}/items", params={"limit": 5}
    )
    if err:
        return dict(name="items_list", collection=col_id, success=False,
                    duration=dur, http_status=0, msg=f"network error: {err}", extras={})
    ok, reason, _ = _validate_response(resp)
    extras = {}
    if ok:
        try:
            features = resp.json().get("features", [])
            extras["item_count"] = len(features)
            for feat in features:
                for asset in feat.get("assets", {}).values():
                    href = asset.get("href", "")
                    if href.startswith("http"):
                        extras["sample_asset_url"] = href
                        break
                if "sample_asset_url" in extras:
                    break
        except Exception:
            pass
    return dict(name="items_list", collection=col_id, success=ok,
                duration=dur, http_status=code, msg=reason, extras=extras)

def probe_search(col_id: str) -> ProbeResult:
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "limit": 5,
        "datetime": f"{start}/{end}",
        "collections": [col_id],
    }
    resp, dur, code, err = _timed_post(
        f"{STAC_URL}/search",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    if err:
        return dict(name="search_post", collection=col_id, success=False,
                    duration=dur, http_status=0, msg=f"network error: {err}", extras={})
    ok, reason, _ = _validate_response(resp)
    extras = {}
    if ok:
        try:
            body = resp.json()
            extras["matched"]  = body.get("context", {}).get("matched",
                                  body.get("numberMatched", "?"))
            extras["returned"] = len(body.get("features", []))
        except Exception:
            pass
    return dict(name="search_post", collection=col_id, success=ok,
                duration=dur, http_status=code, msg=reason, extras=extras)

def probe_asset(col_id: str, asset_url: str) -> ProbeResult:
    if not asset_url:
        return dict(name="asset_fetch", collection=col_id, success=True,
                    duration=0, http_status=0,
                    msg="skipped — no asset URL in items", extras={})
    resp, dur, code, err = _timed_head(asset_url)
    if err:
        return dict(name="asset_fetch", collection=col_id, success=False,
                    duration=dur, http_status=0, msg=f"network error: {err}", extras={})
    if code == 405:
        return dict(name="asset_fetch", collection=col_id, success=True,
                    duration=dur, http_status=405,
                    msg="warning — HEAD not supported (405)", extras={})
    if code in (401, 403):
        return dict(name="asset_fetch", collection=col_id, success=True,
                    duration=dur, http_status=code,
                    msg=f"warning — asset requires auth ({code}), URL reachable", extras={})
    ok = 200 <= code < 300
    return dict(name="asset_fetch", collection=col_id, success=ok,
                duration=dur, http_status=code,
                msg="OK" if ok else f"HTTP {code}", extras={})


def run_all_probes() -> list:
    results = []
    results.append(probe_root())

    r_cols = probe_collections()
    results.append(r_cols)

    col_ids = r_cols["extras"].get("collection_ids", [])
    if not col_ids:
        log.warning("No collection IDs found — skipping per-collection probes")
        return results

    log.info("Found %d collection(s)", len(col_ids))

    for col_id in col_ids:
        r_items = probe_items(col_id)
        asset_url = r_items["extras"].get("sample_asset_url")

        results.append(probe_collection_detail(col_id))
        results.append(r_items)
        results.append(probe_search(col_id))
        results.append(probe_asset(col_id, asset_url))

    return results

def main():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    wall_start = time.time()

    probes = run_all_probes()

    wall_duration = time.time() - wall_start
    # asset_fetch depends on object-storage auth, not STAC API health.
    # Failures are still pushed to Pushgateway for visibility in Grafana,
    # but do not fail the overall run.
    overall_ok = all(p["success"] for p in probes if p["name"] != "asset_fetch")

    log.info("")
    log.info("=" * 72)
    log.info("STAC availability  env=%-6s  overall=%-8s  wall=%.2fs",
             ENV, "SUCCESS" if overall_ok else "FAILURE", wall_duration)
    log.info("=" * 72)

    current_col = None
    for p in probes:
        col = p["collection"]
        if col != current_col:
            current_col = col
            log.info("")
            log.info("  collection: %s", col if col else "(global)")
        icon   = "✓" if p["success"] else "✗"
        status = f"http={p['http_status']}" if p["http_status"] else "        "
        extra  = ""
        if p["extras"].get("item_count") is not None:
            extra = f"  items={p['extras']['item_count']}"
        if p["extras"].get("returned") is not None:
            extra = f"  matched={p['extras'].get('matched','?')}  returned={p['extras']['returned']}"
        log.info("    %s  %-20s  %s  %.3fs  %s%s",
                 icon, p["name"], status, p["duration"], p["msg"], extra)
    log.info("")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"\n{'='*72}\n")
        fh.write(f"{ts}  env={ENV}  overall={'SUCCESS' if overall_ok else 'FAILURE'}"
                 f"  wall={wall_duration:.2f}s\n")
        current_col = None
        for p in probes:
            col = p["collection"]
            if col != current_col:
                current_col = col
                fh.write(f"  --- {col if col else '(global)'} ---\n")
            status = "OK  " if p["success"] else "FAIL"
            fh.write(f"    [{status}] {p['name']:<20}  "
                     f"http={p['http_status']}  dur={p['duration']:.3f}s  {p['msg']}\n")

    log.info("Pushing per-probe metrics ...")
    for p in probes:
        push_probe(
            probe_name=p["name"],
            collection=p["collection"],
            success=p["success"],
            duration=p["duration"],
            http_status=p["http_status"],
        )

    push_e2e_result(SERVICE, overall_ok, wall_duration)
    log.info("pushed roll-up  success=%s  duration=%.2fs", overall_ok, wall_duration)

    # Report asset_fetch issues as warnings (visible but don't fail the run)
    asset_failures = [f"{p['collection']}" for p in probes
                      if p["name"] == "asset_fetch" and not p["success"]]
    if asset_failures:
        log.warning("asset_fetch warnings (not counted as failures): %s",
                    ", ".join(asset_failures))

    if not overall_ok:
        failed = [f"{p['name']}[{p['collection']}]" for p in probes
                  if not p["success"] and p["name"] != "asset_fetch"]
        log.error("Failed: %s", ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
