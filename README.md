# TeslaQuant — Tesla Research Lab

A Python research application for evaluating whether observable recent market conditions help anticipate new explosive upside episodes. This repository contains software and tests. Market datasets, credentials, research notes and server state are excluded.

## Current capabilities

- Streamlit interface: Episodes / Data, Experiments, Evidence and Current Forecast.
- Versioned Parquet datasets, DuckDB analysis and a durable SQLite job ledger.
- A separate worker with leases, retries, content-based job identity and restart recovery.
- Price-only annotation snapshots; unreviewed periods remain unknown.
- A fixed chronological daily benchmark with prior-session features, purged outcome windows and saved reports.
- Verified backup/restore, optional object-storage copies, and authenticated DigitalOcean deployment configuration.

There is no validated live predictor in this release. The model evaluation requires a complete price-only review of the selected historical intervals. Synthetic tests establish software behavior; they do not establish forecasting accuracy.

## Run locally

Use Python 3.12.

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/tesla-lab init
.venv/bin/tesla-lab worker
```

In another terminal:

```bash
.venv/bin/streamlit run app.py --server.address=127.0.0.1
```

Open `http://localhost:8501`. Both processes must use the same `TESLA_DATA_DIR`, which defaults to `var`. Datasets and jobs persist there. Closing the browser does not stop queued work.

Import a private audit archive using the application or `tesla-lab import-audit /path/to/audit.zip`. The review document format is in `docs/review-format.md`. Dataset files and restored server state must be transferred privately, never committed to this repository.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Server deployment

The existing deployment uses Docker Compose with separate app, worker and backup services sharing one persistent data volume. `compose.public.yaml` adds Caddy with password-protected secure access. Keep the app's port private.

Copy `.env.example` to `.env`, restrict its permissions, and supply the hostname, username and a Caddy-generated password hash before `bash deploy/setup.sh public`. The local mode is `bash deploy/setup.sh local`. Provider and storage credentials belong only in the appropriate server service environment.

This repository does not by itself provision a Railway deployment, copy the existing server's datasets or enable live forecasts. Railway deployment settings and persistent storage must be configured before using it there.
