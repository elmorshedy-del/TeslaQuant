"""Consistent snapshots with authenticated transport and verified restoration."""
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time
from tesla_lab.data import digest


def create_backup(ledger, destination):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        # Freeze object references while taking the SQLite backup. Objects never mutate.
        with ledger.connect() as source, sqlite3.connect(stage / "ledger.sqlite3") as target:
            source.backup(target)
        manifest = {"ledger.sqlite3": digest(stage / "ledger.sqlite3")}
        # Only the committed objects referenced by this exact ledger snapshot.
        # Directory scanning would include temporary files that writers unlink.
        with sqlite3.connect(stage / "ledger.sqlite3") as snapshot:
            objects = [(path, checksum) for path, checksum in snapshot.execute("SELECT path,checksum FROM datasets")]
            objects += [(path, rid) for rid, path in snapshot.execute("SELECT id,path FROM raw_objects")]
            for (result,) in snapshot.execute("SELECT result FROM jobs WHERE status='succeeded' AND result IS NOT NULL"):
                rel = json.loads(result).get("report_path")
                if rel:
                    objects.append((rel, Path(rel).stem))
        for rel, expected in sorted(set(objects)):
            path = (ledger.root / rel).resolve()
            if not path.is_relative_to(ledger.root) or not rel.startswith(("datasets/", "raw/", "reports/")):
                raise ValueError("Unsafe committed object path")
            if not path.is_file() or digest(path) != expected:
                raise ValueError("Committed object checksum mismatch")
            manifest[rel] = expected
        (stage / "manifest.json").write_text(json.dumps({"format": 1, "sha256": manifest}, sort_keys=True))
        fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".tar.gz")
        os.close(fd)
        try:
            with tarfile.open(temporary, "w:gz") as tar:
                tar.add(stage / "manifest.json", arcname="manifest.json")
                tar.add(stage / "ledger.sqlite3", arcname="ledger.sqlite3")
                for rel in manifest:
                    if rel != "ledger.sqlite3":
                        tar.add(ledger.root / rel, arcname=rel)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return destination


def restore_backup(archive, target):
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore target must be absent or empty")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        stage = Path(tmp) / "restored"
        stage.mkdir()
        with tarfile.open(archive, "r:gz") as tar:
            names = set()
            for member in tar.getmembers():
                relative = Path(member.name)
                if not member.isfile() or relative.is_absolute() or ".." in relative.parts or member.name in names:
                    raise ValueError("Unsafe backup member")
                names.add(member.name)
            tar.extractall(stage, filter="data")
        manifest = json.loads((stage / "manifest.json").read_text())
        if manifest.get("format") != 1 or names != set(manifest["sha256"]) | {"manifest.json"}:
            raise ValueError("Backup manifest mismatch")
        for rel, expected in manifest["sha256"].items():
            if digest(stage / rel) != expected:
                raise ValueError("Backup checksum mismatch")
        if target.exists():
            target.rmdir()
        os.replace(stage, target)
    return target


def upload_spaces(path):
    import boto3
    required = ["SPACES_ENDPOINT", "SPACES_BUCKET", "SPACES_ACCESS_KEY", "SPACES_SECRET_KEY"]
    if not all(os.environ.get(k) for k in required):
        raise ValueError("Spaces endpoint, bucket and credentials are required")
    endpoint = os.environ["SPACES_ENDPOINT"]
    if not endpoint.startswith("https://"):
        raise ValueError("Spaces endpoint must use HTTPS")
    client = boto3.client("s3", endpoint_url=endpoint, region_name=os.environ.get("SPACES_REGION", "nyc3"),
                          aws_access_key_id=os.environ["SPACES_ACCESS_KEY"], aws_secret_access_key=os.environ["SPACES_SECRET_KEY"])
    key = "tesla-lab/backups/" + Path(path).name
    client.upload_file(str(path), os.environ["SPACES_BUCKET"], key)
    # Compare uploaded object checksum by downloading it; restoration is a separate gate.
    with tempfile.TemporaryDirectory() as tmp:
        downloaded = Path(tmp) / "verify.tar.gz"
        client.download_file(os.environ["SPACES_BUCKET"], key, str(downloaded))
        if digest(downloaded) != digest(path):
            raise ValueError("Remote backup verification failed")
    return {"key": key, "sha256": digest(path)}


def backup_loop(ledger, stop):
    next_snapshot = 0.
    next_upload = 0.
    pending = None
    backoff = 300.
    while not stop.is_set():
        now = time.time()
        if now >= next_snapshot:
            path = ledger.root / "backups" / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".tar.gz")
            try:
                create_backup(ledger, path)
            except Exception as exc:
                logging.error("Local backup failed: %s", type(exc).__name__)
                stop.wait(300)
                continue
            next_snapshot = now + 24 * 60 * 60
            # Bound local disk use even while off-server storage is unavailable.
            for old in sorted(path.parent.glob("*.tar.gz"))[:-7]:
                old.unlink()
            if os.environ.get("SPACES_BUCKET"):
                pending, next_upload, backoff = path, now, 300.
        if pending is not None and now >= next_upload:
            try:
                upload_spaces(pending)
                pending = None
            except Exception as exc:
                logging.error("Remote backup failed; retrying same snapshot: %s", type(exc).__name__)
                next_upload = now + backoff
                backoff = min(backoff * 2, 3600.)
        wake = min(next_snapshot, next_upload) if pending is not None else next_snapshot
        stop.wait(max(1., wake - time.time()))
