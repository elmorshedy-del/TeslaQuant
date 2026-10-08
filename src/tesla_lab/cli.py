import argparse
import json
from pathlib import Path
import signal
import threading
from tesla_lab.config import data_root
from tesla_lab.ledger import Ledger
from tesla_lab.data import Lake
from tesla_lab.worker import run, run_once


def main():
    parser = argparse.ArgumentParser(description="Tesla research infrastructure")
    parser.add_argument("--root", type=Path, default=data_root())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    imp = sub.add_parser("import-audit")
    imp.add_argument("archive", type=Path)
    sub.add_parser("status")
    backup = sub.add_parser("backup")
    backup.add_argument("destination", type=Path)
    sub.add_parser("backup-loop")
    restore = sub.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    review = sub.add_parser("review")
    review.add_argument("document", type=Path)
    args = parser.parse_args()
    if args.command == "restore":
        from tesla_lab.backup import restore_backup
        print(restore_backup(args.archive, args.destination))
        return
    ledger = Ledger(args.root)
    if args.command == "init":
        from tesla_lab.providers import seed_references
        seed_references(ledger)
        print(json.dumps({"status": "initialized", "root": str(ledger.root)}))
    elif args.command == "import-audit":
        if args.archive.stat().st_size > 100 * 1024**2:
            raise ValueError("Audit archive exceeds 100 MB")
        rid = Lake(ledger).raw(args.archive.read_bytes(), {"type": "audit_upload"})
        jid = ledger.enqueue("import_audit", {"path": f"raw/{rid}"})
        print(json.dumps({"job_id": jid, "status": "queued"}))
    elif args.command == "status":
        print(json.dumps({"datasets": ledger.datasets(), "jobs": ledger.jobs()}, indent=2))
    elif args.command == "review":
        from tesla_lab.episodes import save_review
        print(save_review(ledger, json.loads(args.document.read_text())))
    elif args.command == "backup":
        from tesla_lab.backup import create_backup
        print(create_backup(ledger, args.destination))
    else:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        if args.command == "backup-loop":
            from tesla_lab.backup import backup_loop
            backup_loop(ledger, stop)
        elif args.once:
            run_once(ledger)
        else:
            run(ledger, stop)


if __name__ == "__main__":
    main()
