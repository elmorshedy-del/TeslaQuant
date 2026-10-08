"""Run a private Streamlit app, worker and backups behind one authenticated port."""
import hashlib
import hmac
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from tesla_lab.backup import restore_backup


ROOT = Path(os.environ.get("TESLA_DATA_DIR", "/data/state"))
SEED = Path("/app/bootstrap/seed.enc")
AUTH = Path("/app/bootstrap/seed.hmac")
UID = GID = 10001


def seed_if_empty():
    ROOT.mkdir(parents=True, exist_ok=True)
    if (ROOT / "ledger.sqlite3").exists():
        return
    if any(ROOT.iterdir()):
        raise RuntimeError("Data volume has files but no ledger; refusing to overwrite it")
    key = os.environ.get("TESLA_BOOTSTRAP_KEY")
    if not key:
        raise RuntimeError("TESLA_BOOTSTRAP_KEY is required for the first start")
    expected = AUTH.read_text().strip()
    actual = hmac.new(hashlib.sha256(("auth:" + key).encode()).digest(), SEED.read_bytes(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, actual):
        raise RuntimeError("Encrypted seed integrity check failed")
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        archive = temp / "state.tar.gz"
        subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                        "-in", str(SEED), "-out", str(archive), "-pass", "env:TESLA_BOOTSTRAP_KEY"],
                       check=True)
        stage = temp / "stage"
        restore_backup(archive, stage)
        for path in sorted(stage.iterdir(), key=lambda p: p.name == "ledger.sqlite3"):
            shutil.move(str(path), str(ROOT / path.name))
    print("Verified original research snapshot restored", flush=True)


def drop_privileges():
    os.setgroups([])
    os.setgid(GID)
    os.setuid(UID)


def main():
    if not os.environ.get("LAB_PASSWORD") or not os.environ.get("LAB_USERNAME"):
        raise RuntimeError("Password protection must be configured")
    os.environ.setdefault("PORT", "8080")
    seed_if_empty()
    os.chown(ROOT, UID, GID)
    for base, dirs, files in os.walk(ROOT):
        for name in dirs + files:
            os.chown(Path(base) / name, UID, GID)
    ROOT.chmod(0o700)
    hashed = subprocess.run(["caddy", "hash-password"], input=os.environ["LAB_PASSWORD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    if not hashed:
        raise RuntimeError("Password hashing failed")
    env = os.environ.copy()
    env.pop("LAB_PASSWORD", None)
    env.pop("TESLA_BOOTSTRAP_KEY", None)
    env["LAB_PASSWORD_HASH"] = hashed
    children = [
        subprocess.Popen(["tesla-lab", "worker"], env=env, preexec_fn=drop_privileges),
        subprocess.Popen(["tesla-lab", "backup-loop"], env=env, preexec_fn=drop_privileges),
        subprocess.Popen(["streamlit", "run", "/app/app.py", "--server.address=127.0.0.1"],
                         env=env, preexec_fn=drop_privileges),
        subprocess.Popen(["caddy", "run", "--config", "/app/deploy/Caddyfile.railway", "--adapter", "caddyfile"], env=env),
    ]
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError("A required app process exited; restarting the service")
            time.sleep(2)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
