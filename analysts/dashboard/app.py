"""Publish saved monitor results and view them. No model, broker or trading calls."""

import argparse
from datetime import date, datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template
from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from google.cloud import storage

SUBJECTS = {
    s: v["name"]
    for s, v in json.loads((Path(__file__).parent / "securities.json").read_text()).items()
    if v["enabled"]
}
FORECAST_FIELDS = (
    "forecast_id",
    "predecessor_forecast_id",
    "subject",
    "symbol",
    "issued_at",
    "information_cutoff",
    "horizon_end",
    "horizon_sessions",
    "instrument_type",
    "quote_unit",
    "reference_price",
    "price_date",
    "price_source",
    "expected_return",
    "expected_end_price",
    "scenarios",
    "calibration",
    "monitor_evidence_mode",
)
PROVIDER_ERRORS = {
    "project_spend_limit_exceeded": "Project API spending limit reached",
    "organization_spend_limit_exceeded": "Organization API spending limit reached",
    "organization_usage_limit_exceeded": "Organization API usage limit reached",
    "credit_balance_exhausted": "API credit balance exhausted",
    "insufficient_quota": "API quota unavailable",
}


# Serialize a publication to stable JSON bytes for storage and checksum verification.
def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


# Parse a publication timestamp and normalize it to UTC.
def instant(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("A timezone is required.")
    return dt.astimezone(timezone.utc)


# Copy the requested fields from a research record into a smaller view.
def select(value, keys):
    return {key: value[key] for key in keys if key in value}


# Read one completed monitor cycle and assemble the record the dashboard publishes.
def load_cycle(folder, symbol):
    """Use a terminal cycle, not a summary or an unaccepted model draft."""
    folder = Path(folder)
    raw = (folder / "result.json").read_bytes()
    result = json.loads(raw)
    if symbol not in SUBJECTS or result.get("symbol") != symbol:
        raise ValueError("Cycle and requested subject must match.")
    if result.get("status") not in ("completed", "failed"):
        raise ValueError("Only a terminal cycle can be published.")
    cutoff = instant(result["as_of"])
    request_path = folder / "request.json"
    payload = (
        json.loads(json.loads(request_path.read_text())["input"]) if request_path.exists() else {}
    )
    forecast = payload.get("forecast", {})
    if forecast and (forecast["symbol"] != symbol or payload["as_of"] != result["as_of"]):
        raise ValueError("Request, forecast and result identity differ.")
    record = dict(
        schema="monitor-publication-v1",
        symbol=symbol,
        cycle=result["cycle"],
        status="requires_review" if result.get("requires_review") else result["status"],
        as_of=result["as_of"],
        received_at=result.get("received_at"),
        source_result_sha256=sha256(raw).hexdigest(),
        source_request_sha256=sha256(request_path.read_bytes()).hexdigest() if payload else None,
        forecast=select(forecast, FORECAST_FIELDS),
        evidence_mode=forecast.get("monitor_evidence_mode", "unknown"),
        cost=select(result.get("cost", {}), ("total_estimate_usd", "note")),
        model=result.get("execution", {}).get("model"),
        note=result.get("note", ""),
        error_type=result.get("error_type"),
        review_required=bool(result.get("requires_review")),
    )
    if result["status"] == "completed":
        before = json.loads((folder / "state-before.json").read_text())
        after = json.loads((folder / "state-after.json").read_text())
        before_hash = sha256(encode(before).encode()).hexdigest()
        if (
            not forecast.get("forecast_id")
            or not record["received_at"]
            or instant(record["received_at"]) < cutoff
            or instant(forecast["issued_at"]) > cutoff
            or before_hash != result["state_before_sha256"]
            or before_hash != payload["state_before_sha256"]
            or after != result["next_state"]
            or after.get("forecast_id") != forecast["forecast_id"]
            or after.get("last_completed_cycle_at") != result["as_of"]
            or record["review_required"]
        ):
            raise ValueError("Completed result is not bound to its accepted state and baseline.")
        observation = result["observation"]
        if (
            observation["decision"].get("case_id") != result["cycle"]
            or result["handoff"].get("execute") is not False
            or result["alert"].get("execute") is not False
        ):
            raise ValueError("Require a matching, non-executing monitor decision.")
        record.update(
            decision=select(
                observation["decision"],
                (
                    "decision",
                    "reason",
                    "watch_for",
                    "review_question",
                    "condition_ids",
                    "evidence_ids",
                ),
            ),
            checks=[
                select(c, ("id", "status", "note", "source_urls")) for c in observation["checks"]
            ],
            events=[
                select(
                    e,
                    (
                        "id",
                        "event_key",
                        "change",
                        "text",
                        "published_at",
                        "publication_label",
                        "source_urls",
                    ),
                )
                for e in observation["events"]
            ],
            action=result["handoff"]["action"],
            new_referral=result["alert"]["new"],
            coverage=select(
                result["coverage"],
                ("status", "gaps", "checked_through", "scope", "unverified_findings"),
            ),
            price=select(
                payload.get("price_check", {}),
                (
                    "status",
                    "market_data_mode",
                    "qualified_price_trigger",
                    "context_note",
                    "reasons",
                    "retained_reference",
                    "quote_session",
                    "snapshot_status",
                    "reused_snapshot",
                    "snapshot_retrieved_at",
                ),
            ),
            checkpoints=after.get("checkpoints", {}),
        )
        if "referral_case" in observation:
            record["referral_case"] = observation["referral_case"]
            record["referral_qualification"] = observation.get("referral_qualification")
        snapshot = payload.get("price_check", {}).get("snapshot", {})
        record["price"].update(
            last_price=snapshot.get("reported", {}).get("last", {}).get("price"),
            last_at=snapshot.get("interpreted_last_at"),
            timestamp_interpretation=snapshot.get("timestamp_interpretation"),
        )
    if recovery := result.get("polling_recovery"):
        original = (folder / "polling-original-result.json").read_bytes()
        failed = json.loads(original)
        if (
            recovery.get("status") != "response_reconciled"
            or recovery.get("model_requests") != 0
            or recovery.get("provider_status") != result["status"]
            or recovery.get("original_result_sha256") != sha256(original).hexdigest()
            or failed.get("cycle") != result["cycle"]
            or failed.get("as_of") != result["as_of"]
            or failed.get("symbol") != symbol
            or failed.get("phase") != "model_polling"
            or failed.get("status") != "failed"
            or not failed.get("requires_review")
            or not result.get("response_id")
            or failed.get("response_id") != result["response_id"]
            or recovery.get("response_id") != result["response_id"]
        ):
            raise ValueError("Recovered publication must identify its original unresolved result.")
        if result["status"] == "failed":
            response = json.loads((folder / "polling-response.json").read_text())
            error = response.get("error") or {}
            if (
                record["review_required"]
                or result.get("phase") != "provider_failure"
                or response.get("id") != result["response_id"]
                or response.get("status") != "failed"
                or error.get("code") != "server_is_overloaded"
                or result.get("provider_error", {}).get("code") != error["code"]
            ):
                raise ValueError("Recovered failure lacks a matching terminal overload.")
        record["recovered_from_result_sha256"] = recovery["original_result_sha256"]
    return record


class Store:
    """GCS conditional writes; filesystem equivalent solely for offline rehearsal."""

    # Select a local directory or a Cloud Storage bucket as the backing store.
    def __init__(self, uri):
        self.root = None if uri.startswith("gs://") else Path(uri)
        self.bucket = storage.Client().bucket(uri[5:]) if self.root is None else None

    # Read a stored record and its generation so a later write can detect a conflict.
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
        return json.loads(
            blob.download_as_bytes(if_generation_match=int(blob.generation), timeout=30)  # Read the version just inspected.
        ), int(blob.generation)

    # Save a record only if its current generation matches the caller's expectation.
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


# Write the cycle record and advance the subject's published pointers.
def publish(store, record):
    """Record first, then an atomic per-subject pointer; replay never calls research."""
    symbol = record["symbol"]
    key = f"records/{symbol}/{sha256(encode(record).encode()).hexdigest()}.json"
    try:
        store.put(key, record, 0)
    except PreconditionFailed:
        if store.get(key)[0] != record:
            raise ValueError("Existing publication differs.")
    pointer = dict(
        key=key,
        as_of=record["as_of"],
        cycle=record["cycle"],
        status=record["status"],
        forecast_id=record["forecast"].get("forecast_id"),
    )
    index_key = f"targets/{symbol}.json"
    for _ in range(8):
        index, generation = store.get(index_key)
        index = index or {"latest": None, "completed": None}
        for field in ("latest", "completed") if record["status"] == "completed" else ("latest",):
            prior = index[field]
            if prior and instant(prior["as_of"]) == instant(pointer["as_of"]) and prior != pointer:
                previous, _ = store.get(prior["key"])
                if (
                    field != "latest"
                    or record["status"] not in ("completed", "failed")
                    or not record.get("recovered_from_result_sha256")
                    or not previous
                    or previous.get("source_result_sha256")
                    != record["recovered_from_result_sha256"]
                    or previous.get("status") != "requires_review"
                    or previous.get("symbol") != symbol
                    or prior["cycle"] != pointer["cycle"]
                    or prior["forecast_id"] != pointer["forecast_id"]
                ):
                    raise ValueError(
                        "Conflicting terminal publications at the same cutoff; review before replacing."
                    )
                index[field] = pointer
            if prior is None or instant(prior["as_of"]) < instant(pointer["as_of"]):
                index[field] = pointer
        try:
            store.put(index_key, index, generation)
            return key
        except PreconditionFailed:
            continue
    raise RuntimeError("Index is busy. Re-publish this saved cycle; do not repeat research.")


# Summarize the subject's published research and any issues that need attention.
def research_status(store, symbol, now):
    """Separate the saved monitor recommendation from the latest worker check."""
    unknown = dict(label="Current research status unavailable", checked_at=None, attention=True)
    try:
        status, _ = store.get(f"reassessment-operations/{symbol}.json")
        if not status:
            return unknown
        age = (now - instant(status["checked_at"])).total_seconds()
        if not 0 <= age <= 900:
            return dict(
                label="Research status is stale", checked_at=status["checked_at"], attention=True
            )
        phase = status["phase"]
        pending = status.get("pending_referrals") or 0
        failure = (status.get("provider_error") or {}).get("code")
        if failure:
            label = PROVIDER_ERRORS.get(failure, "Provider request failed — review required")
            if status.get("window_ended"):
                label = "Test ended — " + label[0].lower() + label[1:]
            attention = True
        elif status.get("window_ended") and phase != "complete":
            label, attention = "Test ended — review required", True
        elif phase in ("awaiting_baseline", "preparing_baseline", "baseline_ready", "scheduled"):
            label, attention = phase.replace("_", " ").capitalize(), False
        elif phase == "reassessing":
            label, attention = "Reassessment running", False
        elif phase in ("held", "paused", "needs_attention"):
            label, attention = "Research held — review required", True
        elif phase == "complete":
            label, attention = (
                ("Test finished — referral remains" if pending else "Test finished"),
                bool(pending),
            )
        elif status.get("comparison_pending"):
            label, attention = "Awaiting baseline comparison", False
        elif pending:
            label, attention = (
                (
                    "Referral waiting — test limit reached"
                    if status.get("reassessment_attempts")
                    else "Referral awaiting reassessment"
                ),
                True,
            )
        else:
            label, attention = "Monitoring — no reassessment queued", False
        return dict(label=label, checked_at=status["checked_at"], attention=attention)
    except Exception:
        return unknown


# Build the dashboard rows from the latest available records for each subject.
def overview(store, now=None, symbols=SUBJECTS):
    now = now or datetime.now(timezone.utc)
    rows = []
    for symbol in symbols:
        row = dict(
            symbol=symbol,
            name=SUBJECTS[symbol],
            latest=None,
            completed=None,
            warning="Not enrolled",
            research=research_status(store, symbol, now),
        )
        try:
            index, _ = store.get(f"targets/{symbol}.json")
            if index:
                for field in ("latest", "completed"):
                    if index[field]:
                        row[field], _ = store.get(index[field]["key"])
                        if row[field] is None:
                            raise ValueError("Published record missing.")
                latest = row["latest"]
                age = (now - instant(latest["as_of"])).total_seconds()
                row.update(age_minutes=max(0, age / 60), warning="")
                if age < 0:
                    row["warning"] = "Clock mismatch"
                elif age > int(os.environ.get("STALE_AFTER_MINUTES", "120")) * 60:
                    row["warning"] = "Stale — no recent published cycle"
                if not row["warning"] and latest.get("coverage", {}).get("unverified_findings"):
                    row["warning"] = "Source verification pending — monitoring continues"
                end = latest["forecast"].get("horizon_end")
                if end and now.astimezone(ZoneInfo("America/New_York")).date().isoformat() > end:
                    row["warning"] = "Baseline expired — refresh required"
                if latest["status"] != "completed":
                    row["warning"] = (
                        "Review required" if latest["review_required"] else "Latest cycle failed"
                    ) + (" · " + row["warning"] if row["warning"] else "")
        except Exception:
            row["warning"] = "Storage unavailable — status unknown"
            row["latest"] = row["completed"] = None
        rows.append(row)
    return rows


app = Flask(__name__)


# Adapt saved research wording for display while preserving the underlying publication.
@app.template_filter("research_copy")
def research_copy(value):
    """Use the published team name in saved research prose. The JSON evidence keeps its original bytes."""
    if not isinstance(value, str):
        return value
    text = re.sub(r"\bBig[ \t\u00a0]+Seven\b", "Equity and Index Research Team", value)
    # Personal directions in an analyst memo refer to the portfolio manager. Keep names in cited articles.
    contact = os.environ.get("PORTFOLIO_REPORT_CONTACT")
    if not contact:
        return text
    return re.sub(
        r"\b([Ff])or " + re.escape(contact) + r"(?=\s*(?:[,:;.!?]|to\b|$))",
        lambda m: m[1] + "or the portfolio manager",
        text,
    )


# Translate a stored reason code into a label for the dashboard.
@app.template_filter("reason_label")
def reason_label(value):
    return PROVIDER_ERRORS.get(value, value.replace("_", " "))


# Format a stored timestamp for readers in US Eastern time.
@app.template_filter("eastern")
def eastern(value):
    return (
        instant(value).astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %Y · %I:%M %p %Z")
        if value
        else "Not recorded"
    )


# Return a link only when its address is suitable for display in research.
@app.template_filter("safeurl")
def safeurl(value):
    parsed = urlsplit(value)
    return (
        value
        if parsed.scheme in ("https", "http")
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        else None
    )


# Read the subject overview from the report store configured for this service.
def current_rows():
    uri = os.environ.get("REPORT_STORE")
    if not uri:
        abort(503, "Report storage is not configured.")
    return overview(Store(uri))


# Look up the published status of the monitoring workers.
def fleet_status():
    try:
        return Store(os.environ["REPORT_STORE"]).get("fleet-operations.json")[0]
    except Exception:
        return dict(phase="status_unavailable", subjects={}, reasons=["Status could not be read."])


# Render the research overview with coverage for the configured subjects.
@app.get("/")
def home():
    return render_template(
        "dashboard.html", rows=current_rows(), selected=None, fleet=fleet_status()
    )


# Render the research dashboard for a single selected subject.
@app.get("/target/<symbol>")
def detail(symbol):
    if symbol not in SUBJECTS:
        abort(404)
    rows = current_rows()
    review, operations, review_error = None, None, False
    store = Store(os.environ["REPORT_STORE"])
    try:
        operations, _ = store.get(f"reassessment-operations/{symbol}.json")
        _, review = active_report(store, symbol)
    except Exception:
        review, operations, review_error = None, None, True
    return render_template(
        "dashboard.html",
        rows=rows,
        selected=next(r for r in rows if r["symbol"] == symbol),
        reassessment=review,
        reassessment_operations=operations,
        reassessment_error=review_error,
        fleet=fleet_status(),
    )


# Resolve the subject's current report pointer to its publication key and record.
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


# Gather the selected reports and their monitoring status into a portfolio research packet.
def portfolio_research(store, symbols=SUBJECTS, fleet=None, now=None):
    now = now or datetime.now(timezone.utc)
    reports = {}
    for row in overview(store, now, symbols):
        symbol = row["symbol"]
        try:
            key, record = active_report(store, symbol)
            status = "available" if record else "missing"
        except Exception:
            key, record, status = None, None, "unavailable_or_unverified"
        forecast = record["forecast"] if record else {}
        item = dict(
            symbol=symbol,
            name=row["name"],
            status=status,
            report_key=key,
            report_markdown=record["report"] if record else None,
            forecast=select(forecast, (*FORECAST_FIELDS, "model_allocation")) if record else None,
            publication_warning=row["warning"],
            research_status=row["research"],
        )
        if record:
            horizon = forecast.get("horizon_end")
            item["forecast_expired"] = (
                now.astimezone(ZoneInfo("America/New_York")).date().isoformat() > horizon
                if horizon
                else None
            )
            item["coverage_gaps"] = forecast.get("monitoring_handoff", {}).get("coverage_gaps", [])
        for field, source in (
            ("latest_attempt", row["latest"]),
            ("last_completed_monitor", row["completed"]),
        ):
            item[field] = None
            if source:
                monitor = select(
                    source,
                    (
                        "cycle",
                        "symbol",
                        "status",
                        "as_of",
                        "received_at",
                        "model",
                        "source_result_sha256",
                        "coverage",
                        "price",
                        "decision",
                        "checks",
                        "events",
                        "new_referral",
                        "review_required",
                        "note",
                    ),
                )
                monitor["forecast_id"] = source.get("forecast", {}).get("forecast_id")
                monitor["applies_to_active_forecast"] = bool(
                    record and monitor["forecast_id"] == forecast["forecast_id"]
                )
                item[field] = monitor
        if row["latest"] and row["latest"] == row["completed"]:
            item["latest_attempt"] = select(
                item["last_completed_monitor"],
                (
                    "cycle",
                    "status",
                    "as_of",
                    "received_at",
                    "forecast_id",
                    "source_result_sha256",
                    "applies_to_active_forecast",
                ),
            )
        reports[symbol] = item
    return dict(
        schema="portfolio-research-v1",
        generated_at=now.isoformat(),
        execute=False,
        fleet=select(
            fleet or {},
            (
                "test_id",
                "phase",
                "checked_at",
                "starts_at",
                "ends_at",
                "model_profile",
                "monitor_model",
            ),
        ),
        all_reports_available=all(r["status"] == "available" for r in reports.values()),
        subjects=reports,
    )


# Serve portfolio research as JSON for one subject or the full coverage list.
@app.get("/api/portfolio-research", defaults={"symbol": None})
@app.get("/api/portfolio-research/<symbol>")
def api_portfolio_research(symbol):
    if symbol is not None and symbol not in SUBJECTS:
        abort(404)
    if not os.environ.get("REPORT_STORE"):
        abort(503)
    payload = portfolio_research(
        Store(os.environ["REPORT_STORE"]), [symbol] if symbol else SUBJECTS, fleet_status()
    )
    return jsonify(payload), 200 if payload["all_reports_available"] else 503


# Return the original bytes of an archived JSON publication.
def stored_json(key):
    uri = os.environ.get("REPORT_STORE")
    if not uri:
        abort(503)
    try:
        store = Store(uri)
        if store.root is None:
            raw = store.bucket.blob(key).download_as_bytes(timeout=30)
        else:
            raw = (store.root / key).read_bytes()
    except (NotFound, FileNotFoundError):
        abort(404)
    except (GoogleAPICallError, OSError):
        abort(503)
    # The receipt hashes the published bytes, including the JSON's exact whitespace.
    return app.response_class(raw, mimetype="application/json")


# Serve the research completion receipt for the requested calendar date.
@app.get("/api/daily-research/<session_date>/status")
def daily_research_status(session_date):
    try:
        canonical = date.fromisoformat(session_date).isoformat()
    except ValueError:
        abort(404)
    if session_date != canonical:
        abort(404)
    return stored_json(f"daily-research/{session_date}/status.json")


# Serve the frozen seven-report packet for the requested research run.
@app.get("/api/portfolio-research/test/<test_id>")
def portfolio_closeout(test_id):
    if not re.fullmatch(r"securities-\d{8}T\d{6}Z", test_id):
        abort(404)
    return stored_json(f"portfolio-research/{test_id}.json")


# Return the dashboard's current subject rows and worker status as JSON.
@app.get("/api/status")
def api_status():
    return jsonify(
        rows=current_rows(),
        fleet=fleet_status(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# Respond to a service health check.
@app.get("/healthz")
def health():
    return {"viewer": "ready", "worker_health": "not_assessed"}


# Set caching and browser security headers on every dashboard response.
@app.after_request
def headers(response):
    response.headers.update(
        {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        }
    )
    return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-dir", required=True, type=Path)
    parser.add_argument("--symbol", required=True, choices=SUBJECTS)
    parser.add_argument(
        "--store", required=True, help="gs://BUCKET, or a local directory for rehearsal"
    )
    args = parser.parse_args()
    print(publish(Store(args.store), load_cycle(args.cycle_dir, args.symbol)))
