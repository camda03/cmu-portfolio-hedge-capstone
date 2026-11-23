"""Independent five-minute worker check; Cloud Monitoring detects its absence."""

from datetime import timedelta, timezone
import json
import os
from pathlib import Path
import sys
import google.auth
from google.auth.transport.requests import AuthorizedSession

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from monitor import read, stamp, write
from monitor_live import instant
from dashboard.app import Store
from worker.pilot import validate_plan, metric_body


# Summarize the pilot's hourly coverage and flag conditions that require attention.
def observe(folder, now):
    plan = validate_plan(read(folder / "pilot.json"))
    current, start, end = map(instant, (now, plan["starts_at"], plan["ends_at"]))
    health = read(folder / "health.json")
    receipt = folder / "notification-delivery-verified.json"
    delivery = read(receipt) if receipt.exists() else {}
    attempts = sorted((folder / "cycles").glob("*/attempt.json"))[1:]
    finished = [p.parent for p in attempts if (p.parent / "result.json").exists()]
    results = [read(p / "result.json") for p in finished]
    due = []
    slot = start
    while slot < min(current - timedelta(minutes=10), end):
        due.append("hour-" + slot.astimezone(timezone.utc).strftime("%Y%m%dT%H0000Z"))
        slot += timedelta(hours=1)
    missing = sorted(set(due) - {p.parent.name for p in attempts})
    reasons = []
    if health["status"] in ("held", "retry_pending", "paused"):
        reasons.append(health["reason"])
    if (folder / "PAUSED").exists():
        reasons.append("operator_pause")
    running = health["status"] == "running"
    if running and current - instant(health["updated_at"]) > timedelta(minutes=90):
        reasons.append("research_running_over_90_minutes")
    if missing:
        reasons.append("missed_hourly_slot")
    if results:
        latest = finished[-1]
        if read(latest / "ibkr-context.json")["status"] != "received":
            reasons.append("broker_unavailable")
    phase = (
        "scheduled"
        if current < start
        else "complete"
        if current >= end or len(attempts) >= 24
        else "running"
    )
    if running:
        phase = "research_in_progress"
    if reasons:
        phase = "needs_attention"
    return dict(
        schema="deere-pilot-operations-v1",
        pilot_id=plan["pilot_id"],
        checked_at=now,
        starts_at=plan["starts_at"],
        ends_at=plan["ends_at"],
        phase=phase,
        healthy=not reasons,
        reasons=reasons,
        missed_slots=missing,
        new_attempts=len(attempts),
        max_new_attempts=24,
        completed_cycles=sum(r["status"] == "completed" for r in results),
        cost_estimate_usd=sum(r.get("cost", {}).get("total_estimate_usd", 0) for r in results),
        cost_incomplete=len(finished) != len(attempts)
        or any("total_estimate_usd" not in r.get("cost", {}) for r in results),
        worker_status=health["status"],
        worker_updated_at=health["updated_at"],
        notification_delivery_verified=delivery.get("verified") is True
        and delivery.get("pilot_id") == plan["pilot_id"],
        model_requests=0,
    )


# Record the pilot's health and send its observation to Cloud Monitoring.
def check(folder, store, project, now=None, session=None):
    now = now or stamp()
    try:
        value = observe(folder, now)
    except Exception as error:
        plan = read(folder / "pilot.json")
        value = dict(
            schema="deere-pilot-operations-v1",
            pilot_id=plan["pilot_id"],
            checked_at=now,
            phase="needs_attention",
            healthy=False,
            reasons=["worker_check_failed"],
            error_type=type(error).__name__,
            model_requests=0,
        )
    if session is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/monitoring.write"]
        )
        session = AuthorizedSession(credentials)
    response = session.post(
        "https://monitoring.googleapis.com/v3/projects/" + project + "/timeSeries",
        json=metric_body(project, value["pilot_id"], value["healthy"], now),
        timeout=30,
    )
    response.raise_for_status()
    value["metric_write_succeeded"] = True
    write(folder / "operations.json", value)
    key = "operations/DE.json"
    store.put(key, value, store.get(key)[1])
    print(json.dumps(value, sort_keys=True), flush=True)
    return value


if __name__ == "__main__":
    project = os.environ["CAPSTONE_PROJECT"]
    check(Path(os.environ["MONITOR_STATE_DIR"]), Store(os.environ["REPORT_STORE"]), project)
