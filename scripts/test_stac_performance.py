#!/usr/bin/env python3
"""
STAC API load test — headless Locust runner.

Runs four VU stages (10 → 25 → 50 → 100 users, 60 s each) against the STAC
search and items endpoints, then pushes per-endpoint perf metrics to Pushgateway.

Run on demand only (workflow_dispatch in CI)

Environment variables:
  STAC_URL               target API base (default: https://stac.eodc.eu/api/v1)
  E2E_ENV                env label pushed with metrics (default: dev)
  STAC_PERF_COLLECTION   specific collection to probe (default: first collection found)
  PUSHGATEWAY_URL        base URL of the Pushgateway (optional; skip push if unset)
  PUSHGATEWAY_USERNAME   basic-auth username
  PUSHGATEWAY_PASSWORD   basic-auth password
"""

# gevent monkey-patch MUST happen before any other import touches ssl/urllib3
import gevent.monkey
gevent.monkey.patch_all()

import os
import sys
import time
import logging
import requests
import gevent
from locust import HttpUser, task, between
from locust.env import Environment
from locust.log import setup_logging
from prometheus_client import CollectorRegistry, Gauge, generate_latest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STAC_URL    = os.environ.get("STAC_URL", "https://stac.eodc.eu/api/v1")
ENV         = os.environ.get("E2E_ENV", "dev")
SERVICE     = "stac"
PUSHGW_URL  = os.environ.get("PUSHGATEWAY_URL")
PUSHGW_USER = os.environ.get("PUSHGATEWAY_USERNAME")
PUSHGW_PASS = os.environ.get("PUSHGATEWAY_PASSWORD")

VU_STAGES    = [10, 25, 50, 100]
STAGE_SECS   = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resolve probe collection at import time
# ---------------------------------------------------------------------------

def _resolve_collection() -> str:
    override = os.environ.get("STAC_PERF_COLLECTION")
    if override:
        return override
    try:
        r = requests.get(f"{STAC_URL}/collections", timeout=20)
        r.raise_for_status()
        cols = r.json().get("collections", [])
        if cols:
            return cols[0]["id"]
    except Exception as e:
        log.warning("Could not resolve collection from API: %s", e)
    return "unknown"


PROBE_COLLECTION = _resolve_collection()
log.info("Performance probe collection: %s", PROBE_COLLECTION)

# ---------------------------------------------------------------------------
# Locust user
# ---------------------------------------------------------------------------

class StacUser(HttpUser):
    host = STAC_URL
    wait_time = between(0.5, 1.5)

    @task(2)
    def search_post(self):
        self.client.post(
            "/search",
            json={"collections": [PROBE_COLLECTION], "limit": 10},
            name="POST /search",
        )

    @task(1)
    def get_items(self):
        self.client.get(
            f"/collections/{PROBE_COLLECTION}/items",
            params={"limit": 10},
            name="GET /collections/{id}/items",
        )

# ---------------------------------------------------------------------------
# Pushgateway helpers
# ---------------------------------------------------------------------------

def _push(grouping_key: dict, reg: CollectorRegistry):
    if not PUSHGW_URL:
        return
    base = PUSHGW_URL.rstrip("/")
    path = "/".join(f"{k}/{v}" for k, v in sorted(grouping_key.items()))
    url  = f"{base}/metrics/job/e2e_direct/{path}"
    auth = (PUSHGW_USER, PUSHGW_PASS) if PUSHGW_USER and PUSHGW_PASS else None
    try:
        resp = requests.put(
            url, data=generate_latest(reg), auth=auth, timeout=15,
            headers={"Content-Type": "text/plain; version=0.0.4"},
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning("push failed for %s: %s", grouping_key, e)


def _push_stage_metrics(stats_snapshot: dict, vu_count: int):
    now = time.time()
    for endpoint, s in stats_snapshot.items():
        safe = endpoint.replace(" ", "").replace("/", "_").replace("{", "").replace("}", "").strip("_")
        reg = CollectorRegistry()
        Gauge("eodc_e2e_perf_p95_seconds",
              "p95 response time in seconds", registry=reg).set(s["p95"])
        Gauge("eodc_e2e_perf_rps",
              "requests per second", registry=reg).set(s["rps"])
        Gauge("eodc_e2e_perf_error_rate",
              "fraction of failed requests", registry=reg).set(s["err"])
        Gauge("eodc_e2e_perf_vus",
              "virtual user count for this measurement", registry=reg).set(vu_count)
        # include timestamp in every per-endpoint push so it is always reachable
        Gauge("eodc_e2e_perf_last_run_timestamp",
              "unix timestamp of last performance run", registry=reg).set(now)
        _push({"env": ENV, "service": SERVICE, "endpoint": safe}, reg)
        log.info("  pushed  endpoint=%-35s  vu=%3d  p95=%.3fs  rps=%.1f  err=%.3f",
                 endpoint, vu_count, s["p95"], s["rps"], s["err"])

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging("INFO")
    env = Environment(user_classes=[StacUser])
    env.create_local_runner()

    for vu_count in VU_STAGES:
        log.info("Starting stage  VUs=%d  duration=%ds", vu_count, STAGE_SECS)
        env.stats.reset_all()
        env.runner.start(vu_count, spawn_rate=10)
        gevent.sleep(STAGE_SECS)
        env.runner.stop()
        gevent.sleep(1)  # let stats settle

        stats_snapshot = {}
        for (method, name), entry in env.stats.entries.items():
            p95_ms = entry.get_response_time_percentile(0.95) or 0
            stats_snapshot[name] = {
                "p95": p95_ms / 1000.0,
                "rps": entry.total_rps,
                "err": entry.fail_ratio,
            }
            log.info("  [%s] %s  p95=%.3fs  rps=%.1f  errors=%.1f%%",
                     method, name, p95_ms / 1000.0,
                     entry.total_rps, entry.fail_ratio * 100)

        _push_stage_metrics(stats_snapshot, vu_count)

    env.runner.quit()
    log.info("Performance run complete.")


if __name__ == "__main__":
    main()
