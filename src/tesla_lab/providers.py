"""Provider boundaries preserve original responses and publication limitations."""
from io import BytesIO
import hashlib
import json
import os
import re
from pathlib import Path
import time
import zipfile
import pandas as pd
import requests
from tesla_lab.calendar import exchange, session_date
from tesla_lab.data import Lake
from tesla_lab.ledger import canonical, identity


REFERENCE_WEEKS = [
    ("2024-06-24", "summer-2024", "onset_week"), ("2024-07-01", "summer-2024", "continuation_week"),
    ("2024-10-21", "october-2024", "onset_week"), ("2024-11-04", "november-2024", "onset_week"),
    ("2024-12-02", "december-2024", "onset_week"), ("2024-12-09", "december-2024", "continuation_week"),
    ("2025-05-12", "may-2025", "onset_week"), ("2025-09-01", "september-2025", "onset_week"),
    ("2025-09-08", "september-2025", "continuation_week"),
]


def seed_references(ledger):
    with ledger.connect() as c:
        for week, episode, role in REFERENCE_WEEKS:
            body = {"week": week, "episode": episode, "role": role, "status": "unconfirmed_week_identifier"}
            c.execute("INSERT OR IGNORE INTO reference_weeks VALUES(?,?)", (identity(body), canonical(body)))


def reference_annotations(ledger):
    seed_references(ledger)
    with ledger.connect() as c:
        return [json.loads(r[0]) for r in c.execute("SELECT body FROM reference_weeks ORDER BY json_extract(body,'$.week')")]


def import_audit(ledger, archive):
    path = Path(archive).resolve()
    if path.stat().st_size > 100 * 1024**2:
        raise ValueError("Audit archive exceeds 100 MB")
    lake = Lake(ledger)
    blob = path.read_bytes()
    raw_id = lake.raw(blob, {"type": "audit_archive", "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat()})
    with zipfile.ZipFile(BytesIO(blob)) as z:
        if sum(i.file_size for i in z.infolist()) > 128 * 1024**2:
            raise ValueError("Audit expanded size exceeds 128 MB")
        prefix = "tesla_validation/"
        provenance = json.loads(z.read(prefix + "provenance.json"))
        checksums = provenance.get("sha256", {})
        if not isinstance(checksums, dict):
            raise ValueError("Invalid audit checksum manifest")
        for name, expected in checksums.items():
            if not isinstance(name, str) or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise ValueError("Invalid audit checksum entry")
            if prefix + name not in z.namelist():
                raise ValueError(f"Declared audit checksum member missing: {name}")
            if hashlib.sha256(z.read(prefix + name)).hexdigest() != expected:
                raise ValueError(f"Audit checksum mismatch: {name}")
        equity = pd.read_csv(z.open(prefix + "equity_prices.csv"))
        gamma = pd.read_csv(z.open(prefix + "TSLA_gex_source.csv"))
    if "interpolated" in equity:
        if equity.interpolated.astype(str).str.lower().eq("true").any():
            raise ValueError("Interpolated equity data cannot enter the benchmark")
    equity = equity.rename(columns={"date": "session"})
    equity["observed_at"] = equity.session.map(lambda s: exchange().session_close(session_date(s)))
    equity["available_at"] = pd.NaT
    meta = {"provider": "prior_audit", "availability": "retrospective", "price_basis": "split_adjusted",
            "raw_object": raw_id, "provenance": provenance, "transform": "audit-import-v1",
            "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat()}
    eid = lake.put("equity_daily", equity, meta)
    gamma["session"] = pd.to_datetime(gamma["date"].astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    valid = gamma.session.map(lambda s: exchange().is_session(s))
    off_calendar = int((~valid).sum())
    gamma = gamma.loc[valid].copy()
    gamma["symbol"] = "TSLA"
    gamma["gex"] = gamma.gex_total
    gamma["observed_at"] = gamma.session.map(exchange().session_close)
    gamma["available_at"] = pd.NaT
    spot = equity.loc[equity.symbol == "TSLA"].set_index("session").close
    gamma["quality"] = "retrospective_proxy"
    # Raw option underlying and split-adjusted equity have different pre-split bases.
    gamma.loc[gamma.session < "2022-08-25", "quality"] = "unverified_price_basis"
    comparison = gamma.session.map(spot)
    mismatch = (gamma.close / comparison - 1).abs().gt(.01) & gamma.session.ge("2022-08-25")
    gamma.loc[mismatch, "quality"] = "quarantined_spot_mismatch"
    gid = lake.put("gamma_daily", gamma, meta | {"price_basis": "raw", "units": "publisher_defined_gex",
                   "inventory_convention": "calls_positive_puts_negative_proxy_not_dealer_inventory"})
    seed_references(ledger)
    return {"equity_daily": eid, "gamma_daily": gid, "off_calendar_gamma_rows": off_calendar,
            "quarantined_gamma_rows": int(mismatch.sum()), "status": "retrospective_import_only"}


class Alpaca:
    def __init__(self, key=None, secret=None, session=None, raw_sink=None):
        self.key = key or os.environ.get("ALPACA_API_KEY")
        self.secret = secret or os.environ.get("ALPACA_API_SECRET")
        if not self.key or not self.secret:
            raise ValueError("Set ALPACA_API_KEY and ALPACA_API_SECRET in the worker environment")
        self.session = session or requests.Session()
        self.raw_sink = raw_sink

    def fetch_daily(self, symbols, start, end):
        if not symbols or any(s not in {"TSLA", "SPY", "QQQ"} for s in symbols):
            raise ValueError("Daily benchmark supports TSLA/SPY/QQQ")
        if pd.Timestamp(start) > pd.Timestamp(end):
            raise ValueError("Start date follows end date")
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": str(start),
                  "end": (pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat(),
                  "adjustment": "split", "feed": "sip", "limit": 10000, "sort": "asc"}
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        rows, seen_tokens = [], set()
        while True:
            response = self.session.get("https://data.alpaca.markets/v2/stocks/bars", params=dict(params), headers=headers, timeout=60)
            response.raise_for_status()
            now = pd.Timestamp.now(tz="UTC")
            if self.raw_sink:
                self.raw_sink(response.content, {"provider": "alpaca", "endpoint": "stocks/bars", "params": dict(params), "retrieved_at": now.isoformat()})
            body = response.json()
            for symbol, bars in body.get("bars", {}).items():
                for b in bars:
                    label = pd.Timestamp(b["t"]).tz_convert("America/New_York").date().isoformat()
                    observed = exchange().session_close(session_date(label))
                    if observed + pd.Timedelta(minutes=15) > now:
                        raise ValueError("Requested daily bar has not matured; request through a completed session")
                    if label < str(start) or label > str(end):
                        continue
                    rows.append({"symbol": symbol, "session": label, "open": b["o"], "high": b["h"],
                                 "low": b["l"], "close": b["c"], "volume": b["v"], "observed_at": observed,
                                 "available_at": now})
            token = body.get("next_page_token")
            if not token:
                break
            if token in seen_tokens:
                raise ValueError("Provider repeated a pagination token")
            seen_tokens.add(token)
            params["page_token"] = token
        if not rows:
            raise ValueError("Provider returned no daily bars")
        return pd.DataFrame(rows)


def ingest_alpaca(ledger, payload):
    lake = Lake(ledger)
    frame = Alpaca(raw_sink=lake.raw).fetch_daily(payload.get("symbols", ["TSLA", "SPY", "QQQ"]), payload["start"], payload["end"])
    did = lake.put("equity_daily", frame, {"provider": "alpaca_sip", "availability": "verified", "price_basis": "split_adjusted",
                   "historical_revision_status": "backfill_current_split_adjustment_not_as_published",
                   "availability_policy": "observed_available_on_retrieval_only", "transform": "alpaca-daily-v1"})
    return {"dataset_id": did, "rows": len(frame)}
