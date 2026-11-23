"""Configure one 24-hour pilot and its managed alert; never run research."""

from datetime import timedelta, timezone
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor_delta as delta
from monitor import digest, read, stamp, write
from monitor_live import instant, compact

METRIC = "custom.googleapis.com/capstone/worker_ok"
CODE = (
    "worker/runner.py",
    "worker/runtime.py",
    "worker/pilot.py",
    "worker/operations.py",
    "dashboard/app.py",
)


def validate_plan(plan):
    start, end = instant(plan["starts_at"]), instant(plan["ends_at"])
    if (
        plan["schema"] != "deere-hourly-pilot-v1"
        or plan["max_new_attempts"] != 24
        or plan["initial_attempts"] != 1
        or end - start != timedelta(hours=24)
        or start.minute
        or start.second
        or start.microsecond
        or plan["pilot_id"] != "DE-" + start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ):
        raise ValueError("Require the fixed 24-hour, 24-attempt Deere pilot plan.")
    return plan


def plan_at(now):
    start = instant(now).astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) + timedelta(hours=1)
    return dict(
        schema="deere-hourly-pilot-v1",
        pilot_id="DE-" + start.strftime("%Y%m%dT%H%M%SZ"),
        starts_at=start.isoformat(),
        ends_at=(start + timedelta(hours=24)).isoformat(),
        max_new_attempts=24,
        initial_attempts=1,
    )


def activate(folder, plan, old_code):
    validate_plan(plan)
    code = {name: digest((ROOT / name).read_bytes()) for name in CODE}
    with (folder / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = folder / "pilot.json"
        if target.exists():
            saved = read(target)
            if any(saved.get(k) != v for k, v in plan.items()) or saved["code"] != code:
                raise ValueError("Existing pilot cannot be reset or rebound.")
            return saved
        config = read(folder / "worker.json")
        if config["max_attempts"] != 1 or any(
            digest((old_code / n).read_bytes()) != sha for n, sha in config["code"].items()
        ):
            raise ValueError("Original rehearsal source binding differs.")
        attempts = sorted((folder / "cycles").glob("*/attempt.json"))
        if len(attempts) != 1 or read(folder / "health.json")["status"] != "rehearsal_complete":
            raise ValueError("Promote only the completed one-attempt rehearsal.")
        result = read(attempts[0].parent / "result.json")
        state = read(folder / "state.json")
        if (
            result["status"] != "completed"
            or result.get("requires_review")
            or result["next_state"] != state
        ):
            raise ValueError("Accepted completed state must be preserved.")
        watch = delta.verify(folder)
        if instant(plan["ends_at"]) > instant(watch["valid_until"]) or instant(
            plan["ends_at"]
        ) <= instant(stamp()):
            raise ValueError("Pilot window is expired or exceeds the accepted baseline.")
        saved = dict(plan, code=code, initial_state_sha256=digest(compact(state)))
        write(target, saved)
        return saved


def metric_body(project, pilot_id, healthy, now):
    return {
        "timeSeries": [
            {
                "metric": {"type": METRIC, "labels": {"pilot_id": pilot_id}},
                "resource": {"type": "global", "labels": {"project_id": project}},
                "metricKind": "GAUGE",
                "valueType": "DOUBLE",
                "points": [
                    {"interval": {"endTime": now}, "value": {"doubleValue": float(healthy)}}
                ],
            }
        ]
    }


def alert_policy(plan, channel):
    selector = (
        'resource.type="global" AND metric.type="' + METRIC + '"'
        ' AND metric.label.pilot_id="' + plan["pilot_id"] + '"'
    )
    return dict(
        displayName="Deere hourly pilot needs attention",
        enabled=True,
        combiner="OR",
        userLabels={"capstone_pilot": plan["pilot_id"].lower()},
        notificationChannels=[channel],
        conditions=[
            {
                "displayName": "Worker check reports a problem",
                "conditionThreshold": {
                    "filter": selector,
                    "comparison": "COMPARISON_LT",
                    "thresholdValue": 0.5,
                    "duration": "0s",
                    "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_MIN"}],
                    "trigger": {"count": 1},
                },
            },
            {
                "displayName": "Worker check absent for 15 minutes",
                "conditionAbsent": {
                    "filter": selector,
                    "duration": "900s",
                    "trigger": {"count": 1},
                },
            },
        ],
        documentation={
            "mimeType": "text/markdown",
            "content": "The Deere worker is held, a scheduled hour was missed, a broker read failed, or the independent "
            "worker-check heartbeat has been absent for 15 minutes. Check worker/inspect.sh and the private "
            "Research Monitor. An uncertain research request must be reviewed, never repeated blindly. "
            "The 24-hour limit stops new research; the worker check continues until hosting is paused.",
        },
    )


def matching_policy(policy, wanted):
    if (
        not policy.get("enabled", True)
        or policy.get("combiner") != "OR"
        or policy.get("notificationChannels") != wanted["notificationChannels"]
        or len(policy.get("conditions", [])) != len(wanted["conditions"])
    ):
        return False
    for expected in wanted["conditions"]:
        kind = next(k for k in expected if k.startswith("condition"))
        if not any(
            all(
                actual.get(kind, {}).get(k, "0s" if k == "duration" else None) == v
                for k, v in expected[kind].items()
            )
            for actual in policy["conditions"]
        ):
            return False
    return True


def provision(project, folder, email):
    if "@" not in email or email.endswith(".gserviceaccount.com"):
        raise ValueError("Use the operator email address for failure notifications.")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "plan.json"
    plan = validate_plan(read(path)) if path.exists() else plan_at(stamp())
    write(path, plan)  # A retry keeps the same pilot window.
    if instant(plan["ends_at"]) <= instant(stamp()):
        raise ValueError("Saved pilot window expired; do not silently start another pilot.")
    session = requests.Session()
    session.headers["Authorization"] = (
        "Bearer "
        + subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    )
    base = "https://monitoring.googleapis.com/v3/projects/" + project + "/"

    def api(method, tail, body=None, params=None):
        response = session.request(method, base + tail, json=body, params=params, timeout=45)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            token = session.headers["Authorization"].removeprefix("Bearer ")
            message = str(
                error.get("message", "No structured Google error message returned.")
            ).replace(token, "[redacted]")[:2000]
            detail = dict(
                step=method + " " + tail,
                http_status=response.status_code,
                google_status=error.get("status"),
                message=message,
            )
            write(folder / "setup-error.json", detail)
            print(json.dumps(detail), file=sys.stderr, flush=True)
            raise SystemExit(
                "Monitoring setup stopped before VM changes; details saved in setup-error.json."
            ) from None
        return response.json() if response.content else {}

    def listing(tail):
        items = []
        token = None
        while True:
            page = api(
                "GET", tail, params={"pageSize": 1000, **({"pageToken": token} if token else {})}
            )
            items.extend(page.get(tail, []))
            token = page.get("nextPageToken")
            if not token:
                return items

    channels = [
        c
        for c in listing("notificationChannels")
        if c["type"] == "email"
        and c.get("labels", {}).get("email_address") == email
        and c.get("enabled", True)
    ]
    channel = (
        channels[0]
        if channels
        else api(
            "POST",
            "notificationChannels",
            {
                "type": "email",
                "displayName": "Capstone pilot operator",
                "labels": {"email_address": email},
                "enabled": True,
            },
        )
    )
    api(
        "POST",
        "metricDescriptors",
        {
            "type": METRIC,
            "metricKind": "GAUGE",
            "valueType": "DOUBLE",
            "unit": "1",
            "displayName": "Capstone worker check",
            "labels": [{"key": "pilot_id", "valueType": "STRING"}],
        },
    )
    # Seed the series before policy validation. This is a bounded installation
    # grace point, not evidence of a worker check; a missing worker alerts after 15m.
    api("POST", "timeSeries", metric_body(project, plan["pilot_id"], True, stamp()))
    wanted = alert_policy(plan, channel["name"])
    policies = [
        p
        for p in listing("alertPolicies")
        if p.get("userLabels", {}).get("capstone_pilot") == plan["pilot_id"].lower()
    ]
    if len(policies) > 1:
        raise ValueError("Duplicate pilot alert policies need review.")
    policy = policies[0] if policies else api("POST", "alertPolicies", wanted)
    if not matching_policy(policy, wanted):
        raise ValueError("Existing alert policy differs; do not silently replace it.")
    plan.update(alert_policy=policy["name"], notification_channel=channel["name"])
    write(path, plan)
    print(json.dumps(dict(plan, notification_email=email)), flush=True)
    return plan


if __name__ == "__main__":
    if sys.argv[1] == "provision":
        provision(sys.argv[2], Path(sys.argv[3]), sys.argv[4])
    elif sys.argv[1] == "activate":
        print(json.dumps(activate(Path(sys.argv[2]), read(Path(sys.argv[3])), Path(sys.argv[4]))))
    else:
        raise SystemExit("Use provision PROJECT CACHE EMAIL or activate SESSION PLAN OLD_CODE.")
