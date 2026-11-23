"""Coordinate a bounded reassessment and return its accepted report to the monitor."""

import argparse
from contextlib import ExitStack
from datetime import timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor_delta as delta
import monitor_live as live
from monitor import digest, dump, read, stamp, write
from monitor_handoff import (
    check_return,
    check_return_output,
    pending_attention,
    continuity_events,
    discovery_history,
)
from dashboard.app import Store, load_cycle, publish
from securities import SUBJECTS

STAGES = ("business", "valuation", "catalysts", "geoeconomics", "draft", "review", "report")
MODELS = {
    name: live.ASTRA_MODEL if name in ("review", "report") else delta.MODEL for name in STAGES
}
CODE = tuple(
    dict.fromkeys(
        (
            *delta.BASE_FILES,
            "worker/reassessment.py",
            "worker/runtime.py",
            "run.py",
            "outcomes.py",
            "view_run.py",
            "prompts.md",
            "monitor_return_prompt.md",
            "monitor_handoff.py",
            "dashboard/app.py",
            "analyst_discovery.py",
            "analyst_discovery_prompt.md",
            "monitor_baseline_prompt.md",
            "worker/fleet.py",
            "worker/daily_research.py",
            "worker/research_usage.py",
        )
    )
)


# Hash a research record in its compact JSON representation.
def sha(value):
    return digest(live.compact(value))


# Write a new record or confirm that its existing contents are identical.
def immutable(path, value):
    if path.exists() and read(path) != value:
        raise ValueError("Existing checkpoint differs: " + path.name)
    if not path.exists():
        write(path, dump(value))


# Save the watch list and supporting inputs before a worker can use this generation.
def generation(folder, watch, state, report):
    """Commit inputs before making this generation eligible for dispatch."""
    state = dict(state, watch_sha256=sha(watch))
    immutable(folder / "watch.json", watch)
    immutable(folder / "initial-state.json", state)
    immutable(folder / "report.json", report)
    immutable(
        folder / "manifest.json",
        dict(
            model=delta.MODEL,
            settings=live.SETTINGS,
            watch_sha256=sha(watch),
            initial_state_sha256=sha(state),
            report_sha256=sha(report),
        ),
    )
    if not (folder / "state.json").exists():
        write(folder / "state.json", state)


# Check the generation's manifest against the inputs and return its watch list.
def verify(folder, *, model=delta.MODEL):
    manifest, watch = read(folder / "manifest.json"), read(folder / "watch.json")
    if (
        manifest["model"] != model
        or manifest["settings"] != dict(live.SETTINGS, model=model)
        or manifest["watch_sha256"] != sha(watch)
        or manifest["initial_state_sha256"] != sha(read(folder / "initial-state.json"))
        or manifest["report_sha256"] != sha(read(folder / "report.json"))
    ):
        raise ValueError("Generation inputs changed.")
    return watch


# Determine how many hourly checks must pass before dispatch is allowed.
def initial_cycles_required(source, current):
    """A failed predecessor is retained as failed; its replacement proves two slots first."""
    if source["state"].get("requires_review") or source["state"].get("pending_cycle"):
        raise ValueError("Unresolved source state cannot start another test.")
    if source.get("schema") in ("fresh-security-baseline-v1", "continued-security-baseline-v1"):
        if (
            source.get("execute") is not False
            or source.get("validated_seven_stages") is not True
            or source["watch"]["symbol"] not in SUBJECTS
            or source["watch"]["forecast"]["forecast_id"] != source["state"]["forecast_id"]
            or source["state"]["watch_sha256"] != sha(source["watch"])
        ):
            raise ValueError("Fresh baseline identity or validation is missing.")
        if source["schema"] == "continued-security-baseline-v1":
            receipt = source.get("continuation", {})
            if (
                receipt.get("status") != "accepted_state_carried_forward"
                or receipt.get("state_sha256") != sha(source["state"])
                or receipt.get("watch_sha256") != sha(source["watch"])
                or receipt.get("report_sha256") != sha(source["report"])
                or not receipt.get("predecessor_test_id")
                or current < live.instant(receipt["predecessor_ends_at"])
            ):
                raise ValueError("Continuation requires a bound accepted state from an ended test.")
            return 1  # Compare current evidence before dispatching inherited referrals.
        return 0
    if source["status"] == "pilot_complete":
        if (
            current < live.instant(source["ends_at"])
            or source["completed_new_cycles"] != 24
            or source["missed_slots"]
        ):
            raise ValueError("Require the finished, validated 24-cycle pilot; do not interrupt it.")
        return 0
    receipt = source.get("recovery", {})
    if (
        source.get("schema") != "recovered-deere-pilot-export-v1"
        or source["status"] != "recovered_pilot_failed"
        or receipt.get("status") != "response_reconciled"
        or receipt.get("provider_status") != "cancelled"
        or receipt.get("provider_terminal") is not True
        or receipt.get("result_status") != "failed"
        or receipt.get("research_paused") is not True
        or receipt.get("pilot_passed") is not False
        or receipt.get("model_requests") != 0
        or not receipt.get("response_id")
        or receipt.get("pilot_id") != source["monitoring_pilot_id"]
        or source.get("closed_at") != receipt.get("checked_at")
        or current < live.instant(source["closed_at"])
        or source.get("failed_cycles") != [receipt.get("cycle")]
        or source["completed_new_cycles"] < 1
        or source.get("accepted_state_sha256") != sha(source["state"])
    ):
        raise ValueError(
            "Require the validated cancelled-request export, not an unresolved failed pilot."
        )
    return 2


# Import the completed pilot evidence and open a bounded reassessment window.
def prepare(folder, source_file, now=None):
    """Import the completed pilot export once, with a new bounded window."""
    source = read(source_file)
    current = live.instant(now or stamp())
    initial_cycles = initial_cycles_required(source, current)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (folder / "plan.json").exists():
            if read(folder / "plan.json")["source_sha256"] != sha(source):
                raise ValueError("Existing source differs; never reset this test.")
            return
        if (
            list(folder.glob("generations/*/cycles/*/attempt.json"))
            or (folder / "job/attempt.json").exists()
        ):
            raise ValueError("Incomplete preparation has attempts; reconcile it without reset.")
        start = (
            live.instant(source["test_starts_at"])
            if source.get("test_starts_at")
            else current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        )
        symbol = source["watch"]["symbol"]
        plan = dict(
            schema="deere-reassessment-pilot-v1",
            symbol=symbol,
            require_reassessment=source.get(
                "require_reassessment", source.get("schema") != "fresh-security-baseline-v1"
            ),
            pilot_id=symbol + "-return-" + start.strftime("%Y%m%dT%H%M%SZ"),
            starts_at=start.isoformat(),
            ends_at=(start + timedelta(hours=24)).isoformat(),
            max_monitor_attempts=24,
            max_reassessments=1,
            source_sha256=sha(source),
            shared_discovery=dict(model=delta.MODEL, max_attempts_per_reassessment=1),
            minimum_initial_monitor_cycles=initial_cycles,
            predecessor_status=source["status"],
            notification_channel_delivery_previously_verified=source.get(
                "prior_notification_delivery_verified", False
            ),
            monitoring_pilot_id=source["monitoring_pilot_id"],
            execute=False,
            code={n: digest((ROOT / n).read_bytes()) for n in CODE},
        )
        if (
            source["watch"]["forecast"].get("closing_discovery")
            or source.get("schema") == "continued-security-baseline-v1"
        ):
            plan["referral_policy"] = "baseline-comparison-v1"
        immutable(folder / "source.json", source)
        generation(
            folder / "generations/initial", source["watch"], source["state"], source["report"]
        )
        immutable(folder / "active.json", "initial")
        immutable(folder / "plan.json", plan)


# Check that the saved dispatch and terminal response identify the same failed hour.
def settled_overload(target, result, watch, *, model=delta.MODEL):
    """Admit a failed hour only when the saved dispatch, baseline and terminal response agree."""
    if (
        result.get("status") != "failed"
        or result.get("phase") != "provider_failure"
        or (result.get("provider_error") or {}).get("code") != "server_is_overloaded"
        or result.get("requires_review")
        or "next_state" in result
        or (target / "state-after.json").exists()
        or result.get("model_dispatched") is not True
        or result.get("symbol") != watch["symbol"]
    ):
        return False
    attempt = read(target / "attempt.json")
    request = read(target / "request.json")
    payload = json.loads(request["input"])
    # A reconciliation saves the retrieved response before committing its terminal result.
    raw = read(
        target / ("polling-response.json" if result.get("polling_recovery") else "response.json")
    )
    response_id = result.get("response_id")
    dispatched_id = attempt.get("response_id")
    if dispatched_id != response_id and (
        dispatched_id is not None or attempt.get("phase") != "model_request"
    ):
        return False
    return (
        live.confirmed_overload(raw, response_id, request["model"])
        and result.get("provider_error") == live.provider_error(raw)
        and attempt.get("model_dispatched") is True
        and result["cycle"] == attempt.get("cycle") == payload.get("case_id") == target.name
        and result["state_before_sha256"]
        == attempt.get("state_before_sha256")
        == payload.get("state_before_sha256")
        and attempt.get("symbol") == payload["forecast"]["symbol"] == watch["symbol"]
        and result["as_of"] == attempt.get("as_of") == payload["as_of"]
        and request["model"] == model
        and payload["forecast"]["forecast_id"] == watch["forecast"]["forecast_id"]
    )


# Recover the recorded outcome and publication for work that already finished.
def accepted_state(folder, store):
    """Recover terminal state and publication without repeating model submissions."""
    watch = verify(folder)
    state = live.recover_state(folder, watch)
    expected = read(folder / "initial-state.json")
    for attempt in sorted(folder.glob("cycles/*/attempt.json")):
        target = attempt.parent
        if not (target / "result.json").exists():
            raise ValueError("Interrupted monitor attempt requires review.")
        result = read(target / "result.json")
        if read(target / "state-before.json") != expected or result["state_before_sha256"] != sha(
            expected
        ):
            raise ValueError("Monitor state chain changed.")
        if result["status"] != "completed" or result.get("requires_review"):
            if (target / "request.json").exists():
                publish(store, load_cycle(target, watch["symbol"]))
                if settled_overload(target, result, watch):
                    continue  # This attempt consumed the hour. Its forecast and checkpoints did not advance.
            raise ValueError("Failed monitor attempt requires review.")
        expected = result["next_state"]
        immutable(target / "state-after.json", expected)
        publish(store, load_cycle(target, watch["symbol"]))
    if state != expected or state.get("requires_review") or state.get("pending_cycle"):
        raise ValueError("Accepted monitor state changed.")
    return watch, state


# Gather outstanding referrals into the analyst's next reassessment request.
def trigger_for(folder, current, state):
    """Package all unconsidered requests, not a hard-coded historical referral."""
    watch = verify(current)
    history = discovery_history(watch["forecast"])
    events = {item["event"]["id"]: item["event"] for item in history}
    events.update({e["id"]: e for e in read(folder / "source.json")["events"]})
    for path in sorted(folder.glob("generations/*/cycles/*/state-after.json")):
        events.update({e["id"]: e for e in read(path)["prior_events"]})
    events.update({e["id"]: e for e in state["prior_events"]})
    latest = continuity_events(watch["forecast"], state["prior_events"])
    referrals = []
    for ids, attention in sorted(pending_attention(state).items()):
        original = [events[i] for i in attention["evidence_versions"]]
        keys = sorted(set(attention["event_keys"]) | {e["event_key"] for e in original})
        referrals.append(
            dict(
                id="referral-" + sha([ids, attention["evidence_versions"]])[:20],
                condition_ids=list(ids),
                event_keys=keys,
                original_decision=dict(
                    decision="reassess",
                    condition_ids=list(ids),
                    reason=attention["reason"],
                    evidence_ids=attention["evidence_versions"],
                ),
                original_events=original,
                current_events=[latest[k] for k in keys],
            )
        )
    report = read(current / "report.json")
    value = dict(
        schema="monitor-return-v2",
        symbol=watch["symbol"],
        evidence_mode="live_shadow",
        request_id=watch["symbol"] + "-return-" + sha([state["forecast_id"], referrals])[:24],
        as_of=state["last_completed_cycle_at"],
        original_forecast=watch["forecast"],
        original_report=report,
        accepted_handoff=watch["handoff"],
        monitor_state=state,
        referrals=referrals,
        coverage_gaps=state["coverage_gaps"],
        evidence_history=[
            e
            for e in events.values()
            if e["event_key"] in {k for r in referrals for k in r["event_keys"]}
        ],
        price_diagnostics=dict(
            qualified_price_trigger=False, signals=[], execute=False, watch_initialized=False
        ),
        source_provenance=dict(
            accepted_handoff_file_sha256=state["handoff_sha256"],
            monitor_state_sha256=sha(state),
            sources=[],
        ),
    )
    used = {e["id"] for e in value["evidence_history"]}
    value["unverified_evidence_ids"] = sorted(
        item["event"]["id"]
        for item in history
        if item["source_attribution"] == "unverified_lead" and item["event"]["id"] in used
    )
    value.update(
        {
            k + "_sha256": sha(value[k])
            for k in ("original_forecast", "original_report", "accepted_handoff", "monitor_state")
        }
    )
    return value


class PriceCollectionError(ValueError):
    """The daily worker stopped while collecting prices, before the analyst process."""


# Run discovery and the seven analyst stages for the requested subject.
def native(job, trigger=None, symbol=None, *, daily=False):
    """Shared SOL discovery plus the existing seven stages; no automatic retry."""
    symbol = trigger["symbol"] if trigger else symbol
    if symbol not in SUBJECTS:
        raise ValueError("Unknown research subject.")
    from worker.runtime import credentials

    credentials()  # Reuse the VM's already approved dedicated capstone credential.
    # Exclude historical benchmark/reuse overrides, retaining only runtime credentials.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANALYST_", "CRITIC_", "FORECAST_"))
    }
    env.update(
        ANALYST_OUTPUT=str(job / "native"),
        ANALYST_PREVIOUS=str(job / "previous.md"),
        ANALYST_PRICES=str(job / "prices.json"),
        ANALYST_MODEL=MODELS["business"],
        CRITIC_MODEL=MODELS["review"],
        FORECAST_MODEL=MODELS["report"],
        ANALYST_MAX_TOKENS="32000",
        ANALYST_MAX_TOOL_CALLS="50",
        ANALYST_TIMEOUT_SECONDS="1800",
    )
    if daily:
        if live.MODEL_PROFILE != "6.2" or trigger:
            raise ValueError("Daily research requires the approved 6.2 baseline profile.")
        env.update(
            CAPSTONE_DAILY_RESEARCH="1",
            ANALYST_MAX_TOKENS="128000",
            ANALYST_TIMEOUT_SECONDS="7200",
            PYTHONUNBUFFERED="1",
        )
    if trigger:
        write(job / "previous.md", trigger["original_report"])
    resume_kind = "analyst" if (job / "analyst-resume.json").exists() else "discovery"
    resume_path = job / (resume_kind + "-resume.json")
    resume = read(resume_path) if resume_path.exists() else None
    if resume:
        saved = (job / "native" / resume["run"]).resolve()
        if saved.parent != (job / "native").resolve() or any(
            digest((job / name).read_bytes()) != sha for name, sha in resume["files"].items()
        ):
            raise ValueError("Reviewed discovery continuation inputs changed.")
    with (job / "native.log").open("a") as log:
        # A validated latest close is reusable across baseline generations in this test.
        command = "from run import collect_prices; from pathlib import Path; import shutil,sys; shutil.copyfile(collect_prices([sys.argv[3]], cache_dir=Path(sys.argv[2])), sys.argv[1])"
        if not resume:
            try:
                subprocess.run(
                    [
                        "timeout",
                        "--kill-after=5s",
                        "1000s",
                        sys.executable,
                        "-c",
                        command,
                        str(job / "prices.json"),
                        str(job.parent / "market-data"),
                        symbol,
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                    timeout=1010,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                if not daily:
                    raise
                raise PriceCollectionError(
                    f"{symbol}: IBKR price collection stopped before research. See baseline/native.log."
                ) from error
        arguments = (
            [
                "--monitor-trigger",
                str(job / "trigger.json"),
                "--discovery-watch",
                str(job / "discovery-watch.json"),
            ]
            if trigger
            else ["--monitor-baseline"]
        )
        if resume:
            arguments += [
                "--resume-analysts" if resume_kind == "analyst" else "--resume-discovery",
                str(saved),
            ]
        subprocess.run(
            [sys.executable, str(ROOT / "run.py"), symbol, *arguments],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            check=True,
            timeout=None if daily else 7500,
        )
    runs = list((job / "native").glob("*/forecast.json"))
    if len(runs) != 1:
        raise ValueError("Require exactly one native seven-stage result.")
    return runs[0].parent


# Build the next watch list and starting state from the returned analyst forecast.
def successor(watch, state, forecast, trigger):
    handoff, dispositions = check_return_output(forecast, trigger)
    from analyst_discovery import checks_for

    checks = checks_for(watch["symbol"], handoff, watch["checks"])
    mapped = delta.market.forecast_fields(forecast, price_basis="unverified")
    new_watch = dict(
        schema="monitor-successor-watch-v1",
        symbol=watch["symbol"],
        forecast={
            k: v
            for k, v in forecast.items()
            if k not in ("monitoring_handoff", "reassessment_dispositions")
        },
        information_cutoff=forecast["information_cutoff"],
        issued_at=forecast["issued_at"],
        valid_until=mapped["valid_until"],
        handoff=handoff,
        handoff_sha256=digest(dump(handoff)),
        conditions=handoff["conditions"],
        checks=checks,
        baseline_reassessment=dict(
            forecast_id=forecast["forecast_id"],
            predecessor_forecast_id=state["forecast_id"],
            information_cutoff=forecast["information_cutoff"],
            issued_at=forecast["issued_at"],
            dispositions=dispositions,
            original_forecast_sha256=sha(forecast),
        ),
    )
    seed = dict(
        state,
        forecast_id=forecast["forecast_id"],
        handoff_sha256=new_watch["handoff_sha256"],
        collection_floor=forecast["information_cutoff"],
        inherited_discovery_floor=state.get(
            "inherited_discovery_floor", watch["information_cutoff"]
        ),
        considered_referrals=list(state.get("considered_referrals", [])),
    )
    seed.pop("baseline_continuation_completed_at", None)
    incoming = {r["id"]: r for r in trigger["referrals"]}
    for item in dispositions:
        referral = incoming[item["referral_id"]]
        versions = {e["id"] for e in referral["original_events"] + referral["current_events"]}
        seed["considered_referrals"].append(
            dict(
                referral_id=item["referral_id"],
                disposition=item["disposition"],
                condition_ids=item["monitor_condition_ids"],
                source_condition_ids=referral["condition_ids"],
                event_keys=item["incorporated_event_keys"],
                evidence_versions=sorted(versions),
                considered_by_forecast_id=forecast["forecast_id"],
                risk_remains_active=True,
            )
        )
    from analyst_discovery import carry_forward

    carry_forward(new_watch, seed, forecast)
    seed["watch_sha256"] = sha(new_watch)
    return new_watch, seed


# Map each durable output file to its content hash for later verification.
def evidence_files(output):
    return {
        str(p.relative_to(output)): digest(p.read_bytes())
        for p in output.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


# Validate the returned research and publish the next watch generation.
def adopt(folder, store, watch, state, job, output):
    trigger, forecast = read(job / "trigger.json"), read(output / "forecast.json")
    attempt = read(job / "attempt.json")
    if (
        attempt["trigger_sha256"] != sha(trigger)
        or attempt["state_sha256"] != sha(state)
        or read(job / "completed.json")["files"] != evidence_files(output)
    ):
        raise ValueError("Completed reassessment or its bound input changed.")
    report = (output / "report.md").read_text()
    if (
        forecast["forecast_id"] == state["forecast_id"]
        or forecast["symbol"] != watch["symbol"]
        or forecast["status"] != "estimate"
        or forecast["forecast_mode"] != "operational"
        or forecast["predecessor_forecast_id"] != state["forecast_id"]
        or forecast["monitor_trigger_sha256"] != sha(trigger)
        or forecast["monitor_request_id"] != trigger["request_id"]
        or forecast["model_allocation"] != MODELS
        or live.instant(forecast["information_cutoff"]) < live.instant(trigger["as_of"])
        or live.instant(forecast["issued_at"]) < live.instant(forecast["information_cutoff"])
        or not report.startswith("# " + SUBJECTS[watch["symbol"]] + ":")
    ):
        raise ValueError("Candidate is not a bound, operational seven-stage reassessment.")
    if any(not (output / (name + ext)).is_file() for name in STAGES for ext in (".md", ".json")):
        raise ValueError("Native stage evidence is incomplete.")
    if (
        read(output / "monitoring_handoff.json") != forecast["monitoring_handoff"]
        or read(output / "reassessment_dispositions.json") != forecast["reassessment_dispositions"]
    ):
        raise ValueError("Native handoff files differ from the forecast.")
    from analyst_discovery import saved_packet

    discovery = saved_packet(output)
    origin = discovery.get("components", [discovery])[0]
    if (
        forecast.get("shared_discovery") != discovery
        or origin["scope_source"] != "active_monitor_watch"
        or discovery["watch_sha256"] != sha(watch)
        or discovery["monitor_state_sha256"] != sha(state)
        or read(output / "discovery_dispositions.json") != forecast.get("discovery_dispositions")
    ):
        raise ValueError(
            "Shared discovery does not match the saved evidence and bound monitor inputs."
        )
    new_watch, seed = successor(watch, state, forecast, trigger)
    generation(folder / "generations/returned", new_watch, seed, report)
    record = dict(
        schema="research-reassessment-v1",
        symbol=watch["symbol"],
        request_id=trigger["request_id"],
        forecast=forecast,
        report=report,
        execute=False,
        state_before_sha256=sha(state),
        state_after_sha256=sha(seed),
        publication_status="validated_candidate",
        cost_note="Native usage records retained for reconciliation; no combined billing estimate is claimed.",
        stages={
            name: dict(
                text=(output / (name + ".md")).read_text(), response=read(output / (name + ".json"))
            )
            for name in STAGES
        },
    )
    key = "reassessments/" + watch["symbol"] + "/" + sha(record) + ".json"
    existing, version = store.get(key)
    if existing is not None and existing != record:
        raise ValueError("Conflicting reassessment publication.")
    if existing is None:
        store.put(key, record, version)
    immutable(
        job / "accepted.json",
        dict(
            key=key,
            forecast_id=forecast["forecast_id"],
            source_state_sha256=sha(state),
            generation="returned",
            manifest_sha256=sha(read(folder / "generations/returned/manifest.json")),
        ),
    )
    write(
        folder / "active.json", dump("returned")
    )  # Atomic baseline switch, after durable publication.
    store.put(
        "reassessments/" + watch["symbol"] + ".json",
        dict(
            key=key,
            forecast_id=forecast["forecast_id"],
            status="active_for_monitoring",
            execute=False,
        ),
        store.get("reassessments/" + watch["symbol"] + ".json")[1],
    )


# Save and publish the reassessment worker's status and reason.
def health(folder, store, status, reason, **extra):
    plan = read(folder / "plan.json")
    value = dict(
        schema="deere-reassessment-health-v1",
        pilot_id=plan["pilot_id"],
        updated_at=stamp(),
        status=status,
        reason=reason,
        symbol=plan.get("symbol", "DE"),
        active_generation=read(folder / "active.json"),
        execute=False,
        monitor_attempts=len(list(folder.glob("generations/*/cycles/*/attempt.json"))),
        reassessment_attempts=int((folder / "job/attempt.json").exists()),
        **extra,
    )
    value.update(
        minimum_initial_monitor_cycles=plan["minimum_initial_monitor_cycles"],
        completed_initial_monitor_cycles=sum(
            read(p)["status"] == "completed"
            for p in folder.glob("generations/initial/cycles/*/result.json")
        ),
        predecessor_status=plan["predecessor_status"],
    )
    # Keep research attention visible while a new cycle is running, too.
    state_path = folder / "generations" / value["active_generation"] / "state.json"
    try:
        value["pending_referrals"] = len(pending_attention(read(state_path)))
    except (OSError, KeyError, TypeError, ValueError):
        value.update(pending_referrals=None, referral_state_unavailable=True)
    write(folder / "health.json", value)
    store.put(
        "reassessment-health/" + value["symbol"] + ".json",
        value,
        store.get("reassessment-health/" + value["symbol"] + ".json")[1],
    )
    print(json.dumps(value, sort_keys=True), flush=True)
    return value


# Find the hourly interval containing the supplied time within this plan.
def slot_start(plan, clock):
    start = live.instant(plan["starts_at"])
    hour = timedelta(hours=1)
    return start + ((clock - start) // hour) * hour  # Round down relative to the plan start.


# Advance the current hour, including referrals and any eligible reassessment.
def tick(
    folder, store, now=None, client=None, analyst=native, broker=True, dispatch=True, recover=False
):
    if recover and dispatch:
        raise ValueError("Response recovery must not dispatch new research.")
    with (folder / ".worker.lock").open("a") as lock, ExitStack() as resources:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = read(folder / "plan.json")
        source = read(folder / "source.json")
        if (
            plan["schema"] != "deere-reassessment-pilot-v1"
            or plan["max_monitor_attempts"] != 24
            or plan["max_reassessments"] != 1
            or plan["execute"] is not False
            or set(plan["code"]) != set(CODE)
            or live.instant(plan["ends_at"]) - live.instant(plan["starts_at"])
            != timedelta(hours=24)
            or sha(source) != plan["source_sha256"]
            or plan["minimum_initial_monitor_cycles"]
            != initial_cycles_required(source, live.instant(now or stamp()))
            or plan["predecessor_status"] != source["status"]
            or any(digest((ROOT / n).read_bytes()) != s for n, s in plan["code"].items())
        ):
            raise ValueError("Test limits, source or code changed.")
        active = read(folder / "active.json")
        if active not in ("initial", "returned") or (
            active == "returned" and not (folder / "job/accepted.json").exists()
        ):
            raise ValueError("Active generation lacks an acceptance receipt.")
        current = folder / "generations" / active
        clock = live.instant(now or stamp())
        state = read(current / "state.json")
        if state.get("pending_cycle"):
            state = live.recover_state(current, verify(current))
        held = state.get("requires_review") or {}
        if recover or (
            dispatch
            and held.get("response_id")
            and not (folder / "PAUSED").exists()
            and live.instant(plan["starts_at"]) <= clock < live.instant(plan["ends_at"])
        ):
            if client is None:
                from worker.runtime import credentials
                from openai import OpenAI

                credentials()
                client = OpenAI(max_retries=0, timeout=live.HTTP_TIMEOUT)
            recovery = live.reconcile_cycle(
                current,
                client,
                verifier=verify,
                requester=lambda *args: delta.request_for(
                    *args,
                    baseline_report=read(current / "report.json")
                    if plan.get("referral_policy")
                    else None,
                ),
                validator=delta.validate_observation,
                advancer=delta.advance,
                renderer=delta.render,
            )
            print(json.dumps(recovery), flush=True)
            if recovery["status"] not in ("response_reconciled", "nothing_to_recover"):
                return health(
                    folder, store, "held", "provider_result_requires_review", recovery=recovery
                )
            if recover:
                accepted_state(current, store)
                return health(
                    folder, store, "waiting", "saved_response_reconciled", recovery=recovery
                )
        watch, state = accepted_state(current, store)
        clock = live.instant(now or stamp())
        job = folder / "job"
        if (job / "accepted.json").exists():
            receipt = read(job / "accepted.json")
            if receipt["manifest_sha256"] != sha(
                read(folder / "generations/returned/manifest.json")
            ):
                raise ValueError("Accepted generation changed.")
            if read(folder / "active.json") != "returned":
                if receipt["source_state_sha256"] != sha(state):
                    raise ValueError("Reassessment activation source changed.")
                write(folder / "active.json", dump("returned"))
                current = folder / "generations/returned"
                watch, state = accepted_state(current, store)
            store.put(
                "reassessments/" + watch["symbol"] + ".json",
                dict(
                    key=receipt["key"],
                    forecast_id=receipt["forecast_id"],
                    status="active_for_monitoring",
                    execute=False,
                ),
                store.get("reassessments/" + watch["symbol"] + ".json")[1],
            )
        elif (job / "attempt.json").exists():
            if not (job / "completed.json").exists():
                marker = job / "discovery-resume.json"
                if (
                    not marker.exists()
                    or (job / "native" / read(marker)["run"] / "continuation-attempt.json").exists()
                    or not dispatch
                    or clock + timedelta(seconds=8600) >= live.instant(plan["ends_at"])
                ):
                    return health(
                        folder, store, "held", "interrupted_or_failed_reassessment_requires_review"
                    )
                gate_path = Path(
                    os.environ.get("CAPSTONE_REASSESSMENT_LOCK", str(folder / ".resume.lock"))
                )
                gate = resources.enter_context(gate_path.open("a"))
                try:
                    fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return health(
                        folder,
                        store,
                        "waiting",
                        "reviewed_continuation_queued",
                        next_due=(slot_start(plan, clock) + timedelta(hours=1)).isoformat(),
                    )
                trigger = read(job / "trigger.json")
                if read(job / "attempt.json")["state_sha256"] != sha(state):
                    raise ValueError("Reviewed continuation state changed.")
                health(folder, store, "reassessing", "continuing_reviewed_discovery")
                output = analyst(job, trigger).resolve()
                immutable(
                    job / "completed.json",
                    dict(
                        output=str(output.relative_to(job.resolve())), files=evidence_files(output)
                    ),
                )

            output = job / read(job / "completed.json")["output"]
            adopt(folder, store, watch, state, job, output)
            current = folder / "generations/returned"
            watch, state = accepted_state(current, store)
            clock = live.instant(now or stamp())
        if (folder / "PAUSED").exists():
            return health(folder, store, "paused", "operator_pause")
        count = len(list(folder.glob("generations/*/cycles/*/attempt.json")))
        if clock >= live.instant(plan["ends_at"]) or count >= 24:
            return health(
                folder,
                store,
                "complete",
                "bounded_test_finished",
                pending_referrals=len(pending_attention(state)),
            )
        if clock < live.instant(plan["starts_at"]):
            return health(
                folder, store, "scheduled", "waiting_for_start", next_due=plan["starts_at"]
            )
        initial_slots = [
            live.instant(plan["starts_at"]) + timedelta(hours=i)
            for i in range(plan["minimum_initial_monitor_cycles"])
        ]
        initial_paths = [
            folder
            / "generations/initial/cycles"
            / ("hour-" + s.astimezone(timezone.utc).strftime("%Y%m%dT%H0000Z"))
            for s in initial_slots
        ]
        if any(
            clock >= s + timedelta(hours=1) and not (p / "attempt.json").exists()
            for s, p in zip(initial_slots, initial_paths)
        ):
            return health(folder, store, "held", "initial_hourly_slot_missing")
        initial_ready = all(
            (p / "result.json").exists() and read(p / "result.json")["status"] == "completed"
            for p in initial_paths
        )
        analyst_available = True
        comparison_ready = (
            not plan.get("referral_policy")
            or state.get("referral_policy") == plan["referral_policy"]
        )
        if (
            comparison_ready
            and os.environ.get("CAPSTONE_REASSESSMENT_LOCK")
            and pending_attention(state)
            and not (job / "attempt.json").exists()
        ):
            gate = resources.enter_context(Path(os.environ["CAPSTONE_REASSESSMENT_LOCK"]).open("a"))
            try:
                fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                analyst_available = False
        if (
            dispatch
            and comparison_ready
            and analyst_available
            and initial_ready
            and pending_attention(state)
            and not (job / "attempt.json").exists()
            and clock + timedelta(seconds=8600) < live.instant(plan["ends_at"])
        ):
            trigger = trigger_for(folder, current, state)
            check_return(trigger, watch["symbol"], clock)
            immutable(job / "trigger.json", trigger)
            immutable(job / "discovery-watch.json", watch)
            immutable(
                job / "attempt.json",
                dict(
                    started_at=clock.isoformat(),
                    request_id=trigger["request_id"],
                    trigger_sha256=sha(trigger),
                    state_sha256=sha(state),
                    models=MODELS,
                    automatic_retries=0,
                ),
            )
            health(folder, store, "reassessing", "shared_discovery_and_native_seven_stages")
            output = analyst(job, trigger).resolve()
            relative = output.relative_to(job.resolve())
            immutable(
                job / "completed.json", dict(output=str(relative), files=evidence_files(output))
            )
            adopt(folder, store, watch, state, job, output)
            current = folder / "generations/returned"
            watch, state = accepted_state(current, store)
            clock = live.instant(now or stamp())
        if clock >= live.instant(plan["ends_at"]):
            return health(folder, store, "complete", "window_ended_after_reassessment")
        cycle = "hour-" + slot_start(plan, clock).strftime("%Y%m%dT%H0000Z")
        # A baseline change does not give permission for a second monitor call in the same hour.
        prior_slot = list(folder.glob("generations/*/cycles/" + cycle + "/attempt.json"))
        if not dispatch or prior_slot:
            return health(
                folder,
                store,
                "waiting",
                "next_hour",
                pending_referrals=len(pending_attention(state)),
                next_due=(slot_start(plan, clock) + timedelta(hours=1)).isoformat(),
            )
        if client is None:
            from worker.runtime import credentials

            credentials()
        immutable(current / "cycles" / cycle / "state-before.json", state)
        health(folder, store, "monitoring", "fresh_cycle")
        live.run_cycle(
            current,
            cycle,
            now=now,
            client=client,
            broker=broker,
            verifier=verify,
            requester=lambda *args: delta.request_for(
                *args,
                baseline_report=read(current / "report.json")
                if plan.get("referral_policy")
                else None,
            ),
            validator=delta.validate_observation,
            advancer=delta.advance,
            renderer=delta.render,
            broker_cache=folder / "market-data",
        )
        accepted_state(current, store)
    # Re-enter after a new request to dispatch its one allowed reassessment promptly.
    return tick(
        folder, store, now=now, client=client, analyst=analyst, broker=broker, dispatch=dispatch
    )


# Parse the worker command and run the requested preparation or monitoring step.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check", "tick", "recover"))
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--store", default=os.environ.get("REPORT_STORE"))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.session, args.source)
        return
    store = Store(args.store)
    try:
        result = tick(
            args.session, store, dispatch=args.command == "tick", recover=args.command == "recover"
        )
        if result["status"] == "held":
            raise SystemExit(2)
    except (ValueError, KeyError) as error:
        health(
            args.session,
            store,
            "held",
            "validation_requires_review",
            error_type=type(error).__name__,
        )
        raise SystemExit(2) from None
    except Exception as error:
        health(
            args.session,
            store,
            "held",
            "interrupted_or_failed_work_requires_review",
            error_type=type(error).__name__,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
