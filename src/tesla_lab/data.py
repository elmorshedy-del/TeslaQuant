"""Content-addressed objects and explicit measurement contracts."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import duckdb
import numpy as np
import pandas as pd
from tesla_lab.calendar import exchange, session_date
from tesla_lab.ledger import canonical, identity


CONTRACTS = {
    "equity_daily": {"symbol", "session", "open", "high", "low", "close", "volume", "observed_at", "available_at"},
    "gamma_daily": {"symbol", "session", "gex", "observed_at", "available_at", "quality"},
    "flow_daily": {"symbol", "session", "signed_volume", "classification_coverage", "spread", "observed_at", "available_at"},
    "option_chain": {"symbol", "session", "expiry", "strike", "right", "multiplier", "gamma", "delta", "observed_at", "available_at"},
    "calendar_events": {"event_id", "scheduled_at", "available_at", "observed_at", "event_type"},
}


def digest(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def immutable_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("Immutable object conflict")
    finally:
        os.unlink(temp)


def validate(kind, frame, metadata):
    if kind not in CONTRACTS:
        raise ValueError("Unknown dataset contract")
    missing = CONTRACTS[kind] - set(frame.columns)
    if missing or frame.empty:
        raise ValueError(f"Missing fields or empty dataset: {sorted(missing)}")
    if metadata.get("availability") not in {"verified", "retrospective", "unknown"} or not metadata.get("provider"):
        raise ValueError("Provider and availability classification required")
    d = frame.copy()
    if "session" in d:
        d["session"] = d.session.map(session_date)
    for name in ("observed_at", "available_at"):
        # Null available_at is preserved for unknown historical publication times.
        d[name] = pd.to_datetime(d[name], utc=True, errors="raise")
    if d.observed_at.isna().any():
        raise ValueError("Observation timestamps required")
    if metadata["availability"] == "verified" and d.available_at.isna().any():
        raise ValueError("Verified data require availability timestamps")
    if (d.available_at < d.observed_at).any():
        raise ValueError("Availability cannot precede observation")
    keys = {"equity_daily": ["symbol", "session"], "gamma_daily": ["symbol", "session"],
            "flow_daily": ["symbol", "session"], "option_chain": ["symbol", "session", "expiry", "strike", "right", "observed_at"],
            "calendar_events": ["event_id", "available_at"]}[kind]
    if d.duplicated(keys).any():
        raise ValueError("Duplicate records")
    if kind == "equity_daily":
        nums = ["open", "high", "low", "close", "volume"]
        d[nums] = d[nums].apply(pd.to_numeric, errors="raise")
        if not np.isfinite(d[nums].to_numpy()).all() or (d[nums[:-1]] <= 0).any().any() or (d.volume < 0).any():
            raise ValueError("Invalid price or volume")
        if (d.high < d[["open", "close", "low"]].max(axis=1)).any() or (d.low > d[["open", "close", "high"]].min(axis=1)).any():
            raise ValueError("Invalid OHLC bounds")
        for row in d.itertuples():
            if row.observed_at < exchange().session_close(row.session):
                raise ValueError("Daily bar observed before session close")
        if metadata.get("price_basis") not in {"split_adjusted", "raw"}:
            raise ValueError("Explicit price basis required")
    if kind == "gamma_daily" and not metadata.get("inventory_convention"):
        raise ValueError("Gamma inventory convention required")
    if kind in {"gamma_daily", "flow_daily", "option_chain"} and not metadata.get("units"):
        raise ValueError("Measurement units required")
    return d.sort_values(keys).reset_index(drop=True)


class Lake:
    def __init__(self, ledger):
        self.ledger = ledger
        self.root = ledger.root

    def put(self, kind, frame, metadata) -> str:
        d = validate(kind, frame, metadata)
        data_json = json.loads(d.to_json(orient="table", date_format="iso", double_precision=15))
        did = identity({"contract_version": 1, "kind": kind, "frame": data_json, "metadata": metadata})
        rel = f"datasets/{kind}/{did}.parquet"
        path = self.root / rel
        if not path.exists():
            from io import BytesIO
            buffer = BytesIO()
            d.to_parquet(buffer, index=False, compression="zstd")
            immutable_bytes(path, buffer.getvalue())
        checksum = digest(path)
        with self.ledger.connect() as c:
            c.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?,?,?,?,?)", (did, kind, rel, checksum, len(d), canonical(metadata), time.time()))
        return did

    def info(self, did):
        with self.ledger.connect() as c:
            row = c.execute("SELECT * FROM datasets WHERE id=?", (did,)).fetchone()
        if row is None:
            raise ValueError("Dataset version not found")
        return dict(row) | {"metadata": json.loads(row["metadata"])}

    def _path(self, did):
        row = self.info(did)
        path = (self.root / row["path"]).resolve()
        if not path.is_relative_to(self.root) or digest(path) != row["checksum"]:
            raise ValueError("Dataset integrity check failed")
        return path

    def read(self, did) -> pd.DataFrame:
        return pd.read_parquet(self._path(did))

    def query(self, did, sql) -> pd.DataFrame:
        # For trusted application queries only, never arbitrary web-user SQL.
        with duckdb.connect(":memory:") as c:
            statements = c.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError("Read-only analytical query required")
            c.from_parquet(str(self._path(did))).create_view("data")
            return c.execute(sql).df()

    def raw(self, data: bytes, metadata: dict) -> str:
        rid = hashlib.sha256(data).hexdigest()
        rel = f"raw/{rid}"
        immutable_bytes(self.root / rel, data)
        with self.ledger.connect() as c:
            c.execute("INSERT OR IGNORE INTO raw_objects VALUES(?,?,?)", (rid, rel, canonical(metadata)))
        return rid

    def report(self, report: dict) -> str:
        rid = identity(report)
        rel = f"reports/{rid}.json"
        immutable_bytes(self.root / rel, canonical(report).encode())
        return rel
