# scripts/conftest.py
# Pytest plugin hooks for STAC functional tests.
# Accumulates per-test results and pushes eodc_e2e_functional_* metrics
# to Pushgateway at session end via requests.put (APISIX-redirect-safe).

import os
import sys
import time
import requests
from prometheus_client import CollectorRegistry, Gauge, generate_latest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

SERVICE     = "stac"
ENV         = os.environ.get("E2E_ENV", "dev")
PUSHGW_URL  = os.environ.get("PUSHGATEWAY_URL")
PUSHGW_USER = os.environ.get("PUSHGATEWAY_USERNAME")
PUSHGW_PASS = os.environ.get("PUSHGATEWAY_PASSWORD")

_results: list = []  # list of {test, success, duration}


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    _results.append({
        "test":     report.nodeid.split("::")[-1],
        "success":  report.passed,
        "duration": getattr(report, "duration", 0.0),
    })


def pytest_sessionfinish(session, exitstatus):
    if not PUSHGW_URL:
        return
    now = time.time()
    for r in _results:
        reg = CollectorRegistry()
        Gauge("eodc_e2e_functional_success",
              "1 pass 0 fail", registry=reg).set(1 if r["success"] else 0)
        Gauge("eodc_e2e_functional_duration_seconds",
              "test duration seconds", registry=reg).set(float(r["duration"]))
        # include timestamp in every per-test push so it is always reachable
        Gauge("eodc_e2e_functional_last_run_timestamp",
              "unix timestamp of last functional run", registry=reg).set(now)
        _push({"env": ENV, "service": SERVICE, "test": r["test"]}, reg)


def _push(grouping_key: dict, reg: CollectorRegistry):
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
        print(f"[conftest] push failed for {grouping_key}: {e}")
