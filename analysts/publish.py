"""Publish each accepted daily report, the immutable batch, and its dated completion receipt."""

from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage
from .common import ENABLED, MODELS, SECURITIES, digest, immutable, read, stamp, write


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


class Store:
    """Generation-checked writes in Cloud Storage and in the worker’s local staging directory."""

    def __init__(self, uri):
        self.root = None if uri.startswith("gs://") else Path(uri)
        self.bucket = storage.Client().bucket(uri[5:]) if self.root is None else None

    def get(self, key):
        if self.root is not None:
            try:
                raw = (self.root / key).read_bytes()
                return json.loads(raw), sha256(raw).hexdigest()
            except FileNotFoundError:
                return None, 0
        blob = self.bucket.get_blob(key, timeout=30)
        if blob is None:
            return None, 0
        return json.loads(blob.download_as_bytes(if_generation_match=int(blob.generation), timeout=30)), int(
            blob.generation
        )

    def put(self, key, value, generation):
        data = encode(value)
        if self.root is None:
            blob = self.bucket.blob(key)
            blob.cache_control = "private, no-store"
            blob.upload_from_string(
                data, content_type="application/json", if_generation_match=generation, timeout=30
            )
            return
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        with (self.root / ".publication.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.get(key)[1] != generation:
                raise PreconditionFailed("Publication changed concurrently.")
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp.name, path)


def active_report(store, symbol):
    pointer, _ = store.get(f"reassessments/{symbol}.json")
    if not pointer:
        return None, None
    key = pointer["key"]
    if (
        pointer.get("execute") is not False
        or pointer.get("status") != "active_for_monitoring"
        or not re.fullmatch(rf"reassessments/{symbol}/[a-f0-9]{{64}}\.json", key)
    ):
        raise ValueError("Invalid accepted report pointer.")
    record, _ = store.get(key)
    if (
        not record
        or record.get("schema") != "research-reassessment-v1"
        or record.get("symbol") != symbol
        or record.get("execute") is not False
        or record["forecast"].get("symbol") != symbol
        or not record.get("report", "").strip()
        or pointer["forecast_id"] != record["forecast"]["forecast_id"]
        or key != f"reassessments/{symbol}/{sha256(encode(record).encode()).hexdigest()}.json"
    ):
        raise ValueError("The accepted report did not match its publication receipt.")
    return key, record


def portfolio_research(store, fleet):
    """Read the accepted files from the daily worker’s staging area."""
    now = datetime.now(timezone.utc)
    reports = {}
    for symbol in ENABLED:
        key, record = active_report(store, symbol)
        if record is None:
            raise ValueError("Missing accepted subject report: " + symbol)
        forecast = record["forecast"]
        index, _ = store.get(f"targets/{symbol}.json")
        if index and (index.get("latest") or index.get("completed")):
            raise ValueError("Daily publication requires the baseline reports for this run.")
        horizon = forecast.get("horizon_end")
        reports[symbol] = dict(
            symbol=symbol,
            name=SECURITIES[symbol]["name"],
            status="available",
            report_key=key,
            report_markdown=record["report"],
            forecast=forecast,
            latest_attempt=None,
            last_completed_monitor=None,
            forecast_expired=now.astimezone(ZoneInfo("America/New_York")).date().isoformat() > horizon
            if horizon
            else None,
            coverage_gaps=forecast.get("monitoring_handoff", {}).get("coverage_gaps", []),
        )
    return dict(
        schema="portfolio-research-v1",
        generated_at=now.isoformat(),
        execute=False,
        fleet=fleet,
        all_reports_available=True,
        subjects=reports,
    )


def publish_once(store, key, value):
    try:
        store.put(key, value, 0)
    except PreconditionFailed:
        if store.get(key)[0] != value:
            raise ValueError("Different content already exists at " + key)


def publish_packet(root, staged, store, plan, dashboard_url):
    """Verify every report against its accepted pointer before making the batch discoverable."""
    path = root / "portfolio-research.json"
    if not path.exists():
        packet = portfolio_research(
            staged, fleet=dict(test_id=plan["run_id"], phase="completed", model_profile="6.2")
        )
        if not packet["all_reports_available"] or set(packet["subjects"]) != set(ENABLED):
            raise ValueError("The portfolio packet requires seven accepted subject reports.")
        packet.update(
            session_date=plan["session_date"],
            run_id=plan["run_id"],
            source_revision=plan["source_revision"],
            hourly_monitoring=False,
            research_started_at=plan["started_at"],
        )
        for symbol, item in packet["subjects"].items():
            key, record = active_report(staged, symbol)
            forecast = record["forecast"]
            published = read(root / symbol / "baseline/published.json")
            if (
                published != dict(key=key, forecast_id=forecast["forecast_id"])
                or forecast["model_allocation"] != MODELS
                or forecast["shared_discovery"]["scope_source"] != "baseline_and_closing_discovery"
                or not (
                    datetime.fromisoformat(plan["started_at"])
                    <= datetime.fromisoformat(forecast["run_started_at"])
                    <= datetime.fromisoformat(forecast["information_cutoff"])
                    <= datetime.fromisoformat(forecast["issued_at"])
                    <= datetime.fromisoformat(packet["generated_at"])
                )
                or item["last_completed_monitor"] is not None
            ):
                raise ValueError("Research provenance differs from this daily batch for " + symbol)
            item.update(
                forecast=forecast,
                report_sha256=digest(item["report_markdown"]),
                publication_warning=None,
                research_status={"label": "Daily equity and index research published"},
            )
        immutable(path, packet)
    packet = read(path)
    if (
        set(packet["subjects"]) != set(ENABLED)
        or packet["run_id"] != plan["run_id"]
        or packet["source_revision"] != plan["source_revision"]
        or packet["session_date"] != plan["session_date"]
    ):
        raise ValueError("Saved portfolio packet differs from the daily plan.")
    for symbol, item in packet["subjects"].items():
        key, record = active_report(staged, symbol)
        if (
            item["report_key"] != key
            or item["forecast"] != record["forecast"]
            or item["report_markdown"] != record["report"]
            or item["report_sha256"] != digest(record["report"])
        ):
            raise ValueError("Saved report differs from the accepted record for " + symbol)
        publish_once(store, key, record)
    key = "portfolio-research/" + plan["run_id"] + ".json"
    publish_once(store, key, packet)
    alias = "daily-research/" + plan["session_date"] + "/portfolio-research.json"
    publish_once(store, alias, packet)
    return dict(
        packet_key=key,
        session_packet_key=alias,
        packet_sha256=digest(encode(packet)),
        dashboard_url=dashboard_url.rstrip("/") + "/api/portfolio-research/test/" + plan["run_id"],
    )


def publish_completed(root, store, dashboard_url):
    """The dated status names the immutable packet only after all seven subjects are accepted."""
    root = Path(root)
    with (root / ".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = read(root / "plan.json")
        state = read(root / "status.json")
        if (state["run_id"], state["source_revision"], state["session_date"]) != (
            plan["run_id"],
            plan["source_revision"],
            plan["session_date"],
        ):
            raise ValueError("Status and plan refer to different batches.")
        if set(state["subjects"]) != set(ENABLED) or not all(
            state["subjects"][s]["status"] == "accepted" for s in ENABLED
        ):
            raise ValueError("All seven saved reports must be accepted before publication.")
        status_key = f"daily-research/{plan['session_date']}/status.json"
        state.update(
            publish_packet(root, Store(str(root / "staged")), store, plan, dashboard_url),
            accepted=len(ENABLED),
            status="completed",
            updated_at=stamp(),
        )
        write(root / "status.json", state)
        store.put(status_key, state, store.get(status_key)[1])
        return state
