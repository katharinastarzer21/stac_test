# EODC STAC API Monitoring

## Overview

This repo monitors the EODC STAC API across three dimensions:

| Dimension | Script | Schedule |
|---|---|---|
| Availability | `scripts/test_stac_availability.py` | Every 30 minutes |
| Functional correctness | `scripts/test_stac_functional.py` | Nightly at 02:00 UTC |
| Performance under load | `scripts/test_stac_performance.py` | On-demand (manual trigger) |

Metrics are pushed to a **Prometheus Pushgateway**, scraped by Prometheus, and visualised in a **Grafana dashboard** (`grafana/stac_dashboard.json`).

Both production (`https://stac.eodc.eu/api/v1`) and development (`https://dev.stac.eodc.eu/api/v1`) environments are monitored independently.

---

## How it works

### Metric pipeline

```
GitHub Actions (cron / dispatch)
        ↓
  Python script runs
        ↓
  Pushes metrics via HTTP PUT to Pushgateway
  (using requests.put with allow_redirects=True — required because
   the APISIX gateway returns HTTP 301 redirects which the standard
   prometheus_client library does not follow)
        ↓
  Prometheus scrapes Pushgateway
        ↓
  Grafana queries Prometheus → dashboard
```

### GitHub secrets required

| Secret | Used by |
|---|---|
| `PUSHGATEWAY_URL` | All scripts |
| `PUSHGATEWAY_USERNAME` | All scripts |
| `PUSHGATEWAY_PASSWORD` | All scripts |
| `STAC_URL_PROD` | Availability + functional workflows |
| `STAC_URL_DEV` | Availability + functional workflows |
| `STAC_WRITE_TOKEN` | Functional test (ingest test only, skipped if absent) |

---

## Script 1 — Availability (`test_stac_availability.py`)

**Runs:** every 30 minutes via GitHub Actions cron (separate jobs for prod and dev).

**What it does:** For every collection in the STAC catalog, runs 6 probes:

| Probe | Request | Checks |
|---|---|---|
| `root` | GET / | STAC root responds with valid JSON |
| `collections_list` | GET /collections | API lists collections |
| `collection_detail` | GET /collections/{id} | Specific collection loads |
| `items_list` | GET /collections/{id}/items | Items endpoint reachable |
| `search_post` | POST /search `{"collections":[id],"limit":5}` | Search returns results |
| `asset_fetch` | HEAD on first asset href | Asset URL is reachable |

**Asset fetch note:** 401/403 responses are treated as success (asset storage requires auth, which is expected). `asset_fetch` is also excluded from the overall pass/fail result — a failing asset fetch does not fail the monitoring run.

**Metrics pushed** (labels: `env`, `service`, `probe`, `collection`):
- `eodc_e2e_probe_success` — 1 (OK) or 0 (FAIL)
- `eodc_e2e_probe_duration_seconds` — response time in seconds
- `eodc_e2e_probe_http_status` — HTTP status code returned

---

## Script 2 — Functional tests (`test_stac_functional.py` + `conftest.py`)

**Runs:** nightly at 02:00 UTC via GitHub Actions (parallel jobs for prod and dev).

**What it does:** pytest suite that verifies API *behaviour* — not just that endpoints respond, but that they return correct data.

| Test | What it verifies |
|---|---|
| `test_collections_not_empty` | At least one collection exists |
| `test_known_item_exists` | A known item can be fetched by ID |
| `test_search_with_collection_filter` | POST /search with collection filter returns results |
| `test_pagination_no_overlap` | Page 2 of /items shares no items with page 1 |
| `test_ingest_visible_delete` | POST item → GET it → DELETE it (skipped if no write token) |
| `test_asset_href_format` | All asset hrefs use a valid URL scheme (http/https/s3) |

**Note:** These tests use the first available collection internally. The Grafana collection filter does not affect this row.

**Metrics pushed** (labels: `env`, `service`, `test`):
- `eodc_e2e_functional_success` — 1 (PASS) or 0 (FAIL)
- `eodc_e2e_functional_duration_seconds` — test duration in seconds
- `eodc_e2e_functional_last_run_timestamp` — unix timestamp of the run

---

## Script 3 — Performance test (`test_stac_performance.py`)

**Runs:** on-demand only — trigger the `perf_test` workflow manually in GitHub Actions.

**What it does:** Headless [Locust](https://locust.io) load test against two endpoints, with four virtual-user (VU) stages:

| Stage | VUs | Duration |
|---|---|---|
| 1 | 10 | 60 s |
| 2 | 25 | 60 s |
| 3 | 50 | 60 s |
| 4 | 100 | 60 s |

Each VU randomly picks: POST /search (2/3 of requests) or GET /collections/{id}/items (1/3).

After each stage, metrics are pushed per endpoint:

**Metrics pushed** (labels: `env`, `service`, `endpoint`):
- `eodc_e2e_perf_p95_seconds` — 95th percentile response time
- `eodc_e2e_perf_rps` — requests per second
- `eodc_e2e_perf_error_rate` — fraction of failed requests (0–1)
- `eodc_e2e_perf_vus` — VU count for the stage
- `eodc_e2e_perf_last_run_timestamp` — unix timestamp of the run

Endpoint label values: `POST_search`, `GET_collections_id_items`.

---

## Grafana dashboard

Import `grafana/stac_dashboard.json`. Datasource UID must be `prometheus` (hardcoded — do not use `${DS_PROMETHEUS}`).

The dashboard has three rows:

### Availability row

| Panel | What it shows |
|---|---|
| Overall Success | Fraction of probes passing right now (all selected collections) |
| Uptime (24h) | % of 30-min check windows in the last 24h where all probes passed |
| Last Run | When the most recent availability check ran |
| Probe History | State timeline — 6 rows (one per probe type), worst result across selected collections. Red = any collection failing that probe. |
| Collection Health | Table — one row per collection, green OK / red FAIL. Shows which collection is the problem. |
| Failing Probes | Table — only rows where `probe_success == 0`. Shows exactly which probe failed for which collection. Empty when everything is healthy. |
| Probe Duration | Timeseries — average response time per probe type over time. |

The **Collection** variable filters all availability panels. Selecting a specific collection narrows all panels to that collection only.

### Functional row

| Panel | What it shows |
|---|---|
| Test Results | Table — one row per test, green PASS / red FAIL |
| Test Duration | Bar chart — how long each test took |
| Pass Rate | % of tests passing |
| Last Run | When the last functional run happened |

**Note:** The Collection filter does not affect the functional row. These tests run against a fixed collection regardless of the selection.

### Performance row

| Panel | What it shows |
|---|---|
| p95 — POST /search | Latest 95th-percentile response time for search |
| p95 — GET /items | Latest 95th-percentile response time for items |
| Error Rate — POST /search | Fraction of failed search requests |
| Error Rate — GET /items | Fraction of failed items requests |
| VUs | VU count from the last performance run |
| Last Run | When the last performance run happened |
| p95 trend | Timeseries of p95 across multiple runs |
| RPS trend | Timeseries of RPS across multiple runs |

**Note:** The Collection filter does not affect the performance row. Performance tests run against a fixed collection on-demand only.

### Response time thresholds

| Metric | Green | Yellow | Red |
|---|---|---|---|
| p95 response time | < 1 s | 1–3 s | ≥ 3 s |
| Error rate | < 1% | 1–5% | ≥ 5% |
| Uptime (24h) | ≥ 99% | 95–99% | < 95% |
| Pass rate (functional) | 100% | 80–99% | < 80% |

These thresholds are based on general API usability standards (< 1 s feels instant; ≥ 3 s users notice degradation). They should be updated once EODC defines formal SLOs for the STAC API.

---

## Running locally

```bash
# Availability (no push — just prints results)
cd ~/work/stac_test
python scripts/test_stac_availability.py

# Functional tests
pytest scripts/test_stac_functional.py -v

# Performance (push optional)
python scripts/test_stac_performance.py
```

Set environment variables as needed:

```bash
export STAC_URL=https://stac.eodc.eu/api/v1
export E2E_ENV=dev
export PUSHGATEWAY_URL=https://...   # optional; skip push if unset
```

---

## Planned / future work

- **Alerting** — the Jira requirement explicitly asks for "actionable alerts". Currently the team only sees issues by looking at Grafana. Next step: set up Grafana alert rules that send a notification (email/Slack) when probes fail for 2+ consecutive checks or when functional tests fail.
- **SLO definition** — agree on formal service level objectives (e.g., "p95 < 2s, uptime ≥ 99.5%") and update dashboard thresholds to match.
- **Functional tests per collection** — currently functional tests use the first available collection. Could be extended to accept a specific collection via env var (`STAC_FUNCTIONAL_COLLECTION`) and surface this in Grafana.
