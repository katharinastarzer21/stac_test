import gevent.monkey
gevent.monkey.patch_all()

import os
import sys
import time
import random
import logging
import requests
import gevent
from datetime import datetime, timedelta
from locust import HttpUser, task, between
from locust.env import Environment
from locust.log import setup_logging
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from shapely.geometry import Polygon, mapping
from shapely.validation import explain_validity

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Config

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

# Resolve probe collection at import time

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

# Locust user

class StacUser(HttpUser):
    host = STAC_URL
    wait_time = between(1, 3)
    _collections: list = []

    def on_start(self):
        """Fetch all available collections once per VU at startup."""
        resp = self.client.get("/collections", name="GET /collections")
        if resp.status_code == 200:
            self._collections = [
                c["id"] for c in resp.json().get("collections", [])
            ]
        if not self._collections and PROBE_COLLECTION != "unknown":
            self._collections = [PROBE_COLLECTION]

    @task
    def search_post(self):
        """Realistic STAC search: random spatial polygon, datetime range, collection subset."""
        query = {
            "datetime":   self._random_datetime(),
            "intersects": self._random_polygon(),
            "collections": self._random_collections(),
            "limit": 100,
        }
        self.client.post("/search", json=query, name="POST /search")

    @task
    def get_items(self):
        """GET items for a random collection."""
        col = random.choice(self._collections) if self._collections else PROBE_COLLECTION
        self.client.get(
            f"/collections/{col}/items",
            params={"limit": 10},
            name="GET /collections/{id}/items",
        )

# Helpers
  
    @staticmethod
    def _random_datetime() -> str:
        start = datetime(2015, 1, 1)
        end   = datetime(2025, 1, 1)
        delta = int((end - start).total_seconds())
        dates = sorted(
            start + timedelta(seconds=random.randrange(delta)) for _ in range(2)
        )
        return f"{dates[0].isoformat()}Z/{dates[1].isoformat()}Z"

    @staticmethod
    def _random_polygon() -> dict:
        center_lon = random.uniform(-180, 180)
        center_lat = random.uniform(-90,   90)
        pts = []
        for _ in range(random.randint(3, 10)):
            lon = max(min(center_lon + random.uniform(-10, 10), 180), -180)
            lat = max(min(center_lat + random.uniform(-10, 10),  90), -90)
            pts.append((lon, lat))
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        return mapping(poly)

    def _random_collections(self) -> list:
        if not self._collections:
            return []
        k = random.randint(1, min(len(self._collections), 5))
        return random.sample(self._collections, k)

# Pushgateway helpers

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


def _push_all_metrics(all_stages: dict):
    """Push all metrics (p95, rps, error rate, slowdown ratio) in one PUT per (endpoint, vus).

    Combining everything into a single push per grouping key prevents Pushgateway
    from overwriting earlier metrics when the slowdown ratio is pushed separately.
    """
    baseline_vu = VU_STAGES[0]
    baseline = all_stages.get(baseline_vu, {})
    now = time.time()
    for vu_count, stats in all_stages.items():
        for endpoint, s in stats.items():
            safe = endpoint.replace(" ", "").replace("/", "_").replace("{", "").replace("}", "").strip("_")
            base_p95 = baseline.get(endpoint, {}).get("p95", 0)
            ratio = s["p95"] / base_p95 if base_p95 > 0 else 1.0
            reg = CollectorRegistry()
            Gauge("eodc_e2e_perf_p95_seconds",
                  "p95 response time in seconds", registry=reg).set(s["p95"])
            Gauge("eodc_e2e_perf_rps",
                  "requests per second", registry=reg).set(s["rps"])
            Gauge("eodc_e2e_perf_error_rate",
                  "fraction of failed requests", registry=reg).set(s["err"])
            Gauge("eodc_e2e_perf_vus",
                  "virtual user count for this measurement", registry=reg).set(vu_count)
            Gauge("eodc_e2e_perf_slowdown_ratio",
                  f"p95 slowdown vs {baseline_vu}-VU baseline", registry=reg).set(ratio)
            Gauge("eodc_e2e_perf_last_run_timestamp",
                  "unix timestamp of last performance run", registry=reg).set(now)
            _push({"env": ENV, "service": SERVICE, "endpoint": safe, "vus": str(vu_count)}, reg)
            log.info("  pushed  endpoint=%-35s  vu=%3d  p95=%.3fs  rps=%.1f  err=%.3f  slowdown=%.2fx",
                     endpoint, vu_count, s["p95"], s["rps"], s["err"], ratio)

# Main

def main():
    setup_logging("INFO")
    env = Environment(user_classes=[StacUser])
    env.create_local_runner()

    all_stages = {}
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

        all_stages[vu_count] = stats_snapshot

    _push_all_metrics(all_stages)
    env.runner.quit()
    log.info("Performance run complete.")


if __name__ == "__main__":
    main()
