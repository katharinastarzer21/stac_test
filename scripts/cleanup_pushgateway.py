"""
Delete all Pushgateway groups where service=stac.

Usage:
    python scripts/cleanup_pushgateway.py

Reads credentials from environment variables:
    PUSHGATEWAY_URL
    PUSHGATEWAY_USERNAME
    PUSHGATEWAY_PASSWORD
"""

import os
import requests

URL  = os.environ["PUSHGATEWAY_URL"].rstrip("/")
AUTH = (os.environ["PUSHGATEWAY_USERNAME"], os.environ["PUSHGATEWAY_PASSWORD"])

# List all groups
r = requests.get(f"{URL}/api/v1/metrics", auth=AUTH, allow_redirects=True, timeout=20)
r.raise_for_status()

groups = r.json().get("data", [])
stac_groups = [g for g in groups if g.get("labels", {}).get("service") == "stac"]

print(f"Found {len(stac_groups)} stac groups to delete")

for group in stac_groups:
    labels = group.get("labels", {})
    job    = labels.get("job", "e2e_direct")
    path   = "/".join(f"{k}/{v}" for k, v in sorted(labels.items()) if k != "job")
    url    = f"{URL}/metrics/job/{job}/{path}"

    resp = requests.delete(url, auth=AUTH, allow_redirects=True, timeout=15)
    print(f"  {resp.status_code}  {path}")

print("Done.")
