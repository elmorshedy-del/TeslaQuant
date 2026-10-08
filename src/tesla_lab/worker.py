"""Jobs execute independently of a browser session. Handlers are allowlisted."""
import logging
import os
from pathlib import Path
import socket
import threading
import uuid
from tesla_lab.data import Lake
from tesla_lab.providers import import_audit, ingest_alpaca


class Blocked(ValueError):
    """Valid job cannot run until its research/data prerequisites are satisfied."""


def execute(ledger, job):
    kind, payload = job["kind"], job["payload"]
    if kind == "inspect":
        return {"datasets": len(ledger.datasets()), "status": "ready"}
    if kind == "import_audit":
        archive = (ledger.root / payload["path"]).resolve()
        if not archive.is_relative_to(ledger.root / "raw"):
            raise ValueError("Audit path must reference a stored raw object")
        return import_audit(ledger, archive)
    if kind == "ingest_alpaca":
        return ingest_alpaca(ledger, payload)
    if kind == "experiment":
        from tesla_lab.research import run_experiment
        return run_experiment(ledger, payload)
    raise ValueError("Unknown job kind")


def run_once(ledger, worker=None):
    worker = worker or socket.gethostname() + "-" + uuid.uuid4().hex[:8]
    job = ledger.claim(worker)
    if not job:
        return False
    done = threading.Event()
    lost = threading.Event()

    def heartbeats():
        while not done.wait(30):
            try:
                ledger.heartbeat(job["id"], job["token"])
            except ValueError:
                lost.set()
                return

    heartbeat = threading.Thread(target=heartbeats, daemon=True)
    heartbeat.start()
    try:
        result = execute(ledger, job)
        if not lost.is_set():
            ledger.finish(job["id"], job["token"], "succeeded", result)
    except Blocked as exc:
        if not lost.is_set():
            ledger.finish(job["id"], job["token"], "blocked", error=str(exc))
    except Exception as exc:
        # Persist safe errors without provider response bodies or credential values.
        message = str(exc)
        for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "SPACES_ACCESS_KEY", "SPACES_SECRET_KEY"):
            secret = os.environ.get(key)
            if secret:
                message = message.replace(secret, "[redacted]")
        logging.error("Job %s failed: %s", job["id"][:12], type(exc).__name__)
        if not lost.is_set():
            try:
                ledger.finish(job["id"], job["token"], "failed", error=message[:2000])
            except ValueError:
                logging.warning("Job lease lost during failure handling")
    finally:
        done.set()
        heartbeat.join(timeout=2)
    return True


def run(ledger, stop):
    while not stop.is_set():
        if not run_once(ledger):
            stop.wait(2)
