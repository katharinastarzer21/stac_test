import os, time, logging, requests
from datetime import datetime, timezone, timedelta
from prometheus_client import CollectorRegistry, Gauge, generate_latest

STAC_URL    = os.environ.get("STAC_URL", "https://stac.eodc.eu/api/v1")
ENV         = os.environ.get("E2E_ENV", "dev")
TIMEOUT     = 20
PUSHGW_URL  = os.environ.get("PUSHGATEWAY_URL")
PUSHGW_USER = os.environ.get("PUSHGATEWAY_USERNAME")
PUSHGW_PASS = os.environ.get("PUSHGATEWAY_PASSWORD")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)


def request(method, url, **kwargs):
    t0 = time.perf_counter()
    try:
        r = getattr(requests, method)(url, timeout=TIMEOUT, allow_redirects=True, **kwargs)
        return r.status_code, time.perf_counter() - t0, r
    except Exception:
        return 0, time.perf_counter() - t0, None


def push(probe_name, collection, success, duration, status):
    if not PUSHGW_URL:
        return
    reg = CollectorRegistry()
    Gauge("eodc_e2e_probe_success",          "", registry=reg).set(int(success))
    Gauge("eodc_e2e_probe_duration_seconds", "", registry=reg).set(duration)
    Gauge("eodc_e2e_probe_http_status",      "", registry=reg).set(status)
    Gauge("eodc_e2e_probe_last_run_timestamp", "", registry=reg).set(time.time())
    key  = {"env": ENV, "service": "stac", "probe": probe_name, "collection": collection}
    path = "/".join(f"{k}/{v}" for k, v in sorted(key.items()))
    auth = (PUSHGW_USER, PUSHGW_PASS) if PUSHGW_USER else None
    try:
        requests.put(
            f"{PUSHGW_URL.rstrip('/')}/metrics/job/e2e_direct/{path}",
            data=generate_latest(reg), auth=auth, timeout=15,
            headers={"Content-Type": "text/plain; version=0.0.4"},
            allow_redirects=True,
        ).raise_for_status()
    except Exception as e:
        log.warning("push failed: %s", e)


def ok(status):
    return 200 <= status < 300


def run():
    all_ok = True

    # Root
    status, dur, _ = request("get", f"{STAC_URL}/")
    result = ok(status)
    log.info("root              %s  http=%d  %.0fms", "OK" if result else "FAIL", status, dur * 1000)
    push("root", "", result, dur, status)
    all_ok = all_ok and result

    # Collections list
    status, dur, resp = request("get", f"{STAC_URL}/collections")
    result = ok(status)
    log.info("collections_list  %s  http=%d  %.0fms", "OK" if result else "FAIL", status, dur * 1000)
    push("collections_list", "", result, dur, status)
    all_ok = all_ok and result

    if not result:
        log.error("Cannot list collections — skipping per-collection probes")
        return all_ok

    col_ids = [c["id"] for c in resp.json().get("collections", []) if "id" in c]
    log.info("Found %d collections", len(col_ids))

    for col_id in col_ids:

        # Collection detail
        status, dur, _ = request("get", f"{STAC_URL}/collections/{col_id}")
        result = ok(status)
        log.info("  %s  collection_detail  %s  http=%d  %.0fms", col_id, "OK" if result else "FAIL", status, dur * 1000)
        push("collection_detail", col_id, result, dur, status)
        all_ok = all_ok and result

        # Items list
        status, dur, resp = request("get", f"{STAC_URL}/collections/{col_id}/items", params={"limit": 5})
        result = ok(status)
        log.info("  %s  items_list         %s  http=%d  %.0fms", col_id, "OK" if result else "FAIL", status, dur * 1000)
        push("items_list", col_id, result, dur, status)
        all_ok = all_ok and result

        # Search (last 90 days)
        now   = datetime.now(timezone.utc)
        start = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        status, dur, _ = request("post", f"{STAC_URL}/search",
                                 json={"limit": 5, "collections": [col_id],
                                       "datetime": f"{start}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"})
        result = ok(status)
        log.info("  %s  search_post        %s  http=%d  %.0fms", col_id, "OK" if result else "FAIL", status, dur * 1000)
        push("search_post", col_id, result, dur, status)
        all_ok = all_ok and result

        # Asset fetch — find first http asset URL from items
        asset_url = None
        if resp and resp.status_code == 200:
            for feat in resp.json().get("features", []):
                for asset in feat.get("assets", {}).values():
                    if isinstance(asset, dict) and asset.get("href", "").startswith("http"):
                        asset_url = asset["href"]
                        break
                if asset_url:
                    break

        if asset_url:
            status, dur, _ = request("head", asset_url)
            result = ok(status) or status in (401, 403, 405)
            log.info("  %s  asset_fetch        %s  http=%d  %.0fms", col_id, "OK" if result else "FAIL", status, dur * 1000)
            push("asset_fetch", col_id, result, dur, status)
        else:
            log.info("  %s  asset_fetch        skipped — no http asset URL found", col_id)
            push("asset_fetch", col_id, True, 0, 0)

    return all_ok


if __name__ == "__main__":
    success = run()
    if not success:
        raise SystemExit(1)
