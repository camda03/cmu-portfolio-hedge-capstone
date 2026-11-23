"""Render the latest completed worker session and publish its private manager dashboard."""

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from decision_history import complete, history, location
from demo_view import render
from hedge_session import ROOT, marked_book, read_json, session_hours
from ledger import instant, open_book


# Find the most recent eligible closing decision with the required output files.
def latest(root):
    found = []
    for pattern in ("live/*/decision.json", "closing-rehearsals/*/decision.json"):
        for file in root.glob(pattern):
            memo = read_json(file)
            if memo.get("mode") not in ("Astra close rehearsal", "Astra advisory") or not memo.get(
                "response_id"
            ):
                continue
            if (
                not (file.parent / "tracker.json").is_file()
                or not (file.parent / "gdelt-view.json").is_file()
            ):
                continue
            when = instant(memo["completed_at"])
            session = file.parent.name[:10]
            found.append(((session, when), file.parent))
    if not found:
        raise ValueError(
            "No completed Astra session with its original saved inputs was found on this worker."
        )
    return max(found)[1]


# Collect a fresh market observation for tracking a previously recorded hedge.
def observe(root, session, broker_dir):
    from market_snapshot import collect

    now = datetime.now(timezone.utc)
    session_hours(now, session)
    policy = read_json(ROOT / "demo_assets/pilot.example.json")
    with TemporaryDirectory(dir=root) as temp, sqlite3.connect(":memory:") as ledger:
        market = collect(broker_dir, policy, Path(temp), session)
        cutoff = datetime.now(timezone.utc)
        opening, _ = session_hours(cutoff, session)
        marked_book(
            ledger, open_book(ledger), market, cutoff, opening, policy["max_mark_age_seconds"]
        )
        history(location(root), cutoff, market)
    print("Recorded a qualified MES observation for " + session)


# Recover the saved decision history from Cloud Storage when it is available.
def restore_history(root, bucket):
    from google.api_core.exceptions import NotFound

    db = location(root)
    if db.is_file():
        return
    with TemporaryDirectory(dir=root) as temp:
        candidate = Path(temp) / "history.sqlite3"
        try:
            bucket.blob("portfolio-hedge/current/decision-history.sqlite3").download_to_filename(
                str(candidate)
            )
        except NotFound:
            return
        with closing(sqlite3.connect(candidate)) as check:
            if check.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Saved decision history failed the SQLite integrity check.")
        candidate.replace(db)


# Back up the SQLite history with a storage generation check to protect concurrent updates.
def backup_history(root, bucket, now):
    from google.api_core.exceptions import NotFound

    pointer = bucket.blob("portfolio-hedge/current/decision-history.sqlite3")
    try:
        pointer.reload()
        generation = pointer.generation
    except NotFound:
        generation = 0
    with TemporaryDirectory(dir=root) as temp:
        snapshot = Path(temp) / "history.sqlite3"
        with (
            closing(sqlite3.connect(location(root))) as source,
            closing(sqlite3.connect(snapshot)) as dest,
        ):
            source.backup(dest)
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        bucket.blob("portfolio-hedge/history/" + stamp + ".sqlite3").upload_from_filename(
            str(snapshot), if_generation_match=0
        )
        pointer.upload_from_filename(str(snapshot), if_generation_match=generation)


# Publish the dated decision artifacts and update the desk's current view.
def publish(root, blob, gdelt=None):
    from google.api_core.exceptions import NotFound, PreconditionFailed

    folder = latest(root)
    now = datetime.now(timezone.utc)
    complete(folder, location(root), now)
    # An older worker may have saved only the compact analog view. Add a full viewer only for identical evidence.
    if gdelt and (gdelt / "handoff.json").is_file():
        expected = read_json(folder / "request.json")["input_sha256"]["gdelt"]
        if sha256((gdelt / "handoff.json").read_bytes()).hexdigest() != expected:
            gdelt = None
    render(folder, gdelt if gdelt and (gdelt / "handoff.json").is_file() else None)
    backup_history(root, blob.bucket, now)
    page = folder / "index.html"
    memo = read_json(folder / "decision.json")
    # Preserve a newer saved session if another worker publishes between read and upload.
    order = folder.name[:10] + "/" + instant(memo["completed_at"]).isoformat()
    for attempt in range(3):
        try:
            blob.reload()
            previous = blob.metadata or {}
            if (previous.get("decision_order", ""), previous.get("history_as_of", "")) > (
                order,
                now.isoformat(),
            ):
                return
            generation = blob.generation
        except NotFound:
            generation = 0
        blob.metadata = {"decision_order": order, "history_as_of": now.isoformat()}
        blob.cache_control = "private, no-store"
        blob.content_disposition = "inline"
        try:
            blob.upload_from_filename(
                str(page),
                content_type="text/html; charset=utf-8",
                if_generation_match=generation,
                timeout=90,
            )
            return
        except PreconditionFailed:
            if attempt == 2:
                raise


if __name__ == "__main__":
    from google.auth.compute_engine import Credentials
    from google.cloud import storage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--gdelt", type=Path)
    parser.add_argument("--restore-only", action="store_true")
    parser.add_argument("--observe", metavar="NYSE_SESSION")
    parser.add_argument("--broker-dir", type=Path)
    args = parser.parse_args()
    root = args.root
    store = storage.Client(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        credentials=Credentials(scopes=["https://www.googleapis.com/auth/devstorage.read_write"]),
    )
    bucket = store.bucket(os.environ["HEDGE_RESEARCH_STORE"].removeprefix("gs://").rstrip("/"))
    restore_history(root, bucket)
    if args.restore_only:
        raise SystemExit(0)
    if args.observe:
        observe(root, args.observe, args.broker_dir)
    name = "portfolio-hedge/current/index.html"
    publish(root, bucket.blob(name), args.gdelt)
    print("Manager dashboard: https://storage.cloud.google.com/" + bucket.name + "/" + name)
