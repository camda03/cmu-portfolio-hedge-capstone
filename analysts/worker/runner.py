"""One durable Deere rehearsal, using the existing monitor and publication code."""

import argparse
from datetime import timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor_delta as delta
import monitor_live as live
from monitor import digest, read, stamp, write
from dashboard.app import Store, load_cycle, publish

SEED_SHA = os.environ.get("MONITOR_SEED_SHA256")
SEED_FILES = ("result.json", "request.json", "state-before.json", "state-after.json")


def prepare(folder, seed):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = load_cycle(seed, "DE")
        if (
            not SEED_SHA
            or record["status"] != "completed"
            or record["source_result_sha256"] != SEED_SHA
        ):
            raise ValueError("Use the reviewed successor-1 cycle as the origin.")
        watch, _, _ = delta.inputs(ROOT, successor=True)
        state = read(seed / "state-after.json")
        if state["watch_sha256"] != digest(live.compact(watch)) or state.get("requires_review"):
            raise ValueError("Origin is not an accepted state for this watch.")
        config = dict(
            schema="deere-worker-rehearsal-v1",
            max_attempts=1,
            origin={n: digest((seed / n).read_bytes()) for n in SEED_FILES},
            code={
                n: digest((ROOT / n).read_bytes())
                for n in ("worker/runner.py", "worker/runtime.py", "dashboard/app.py")
            },
        )
        if (folder / "worker.json").exists():
            if read(folder / "worker.json") != config:
                raise ValueError(
                    "Existing worker binding differs; never reset an attempted session."
                )
            return
        if list((folder / "cycles").glob("*/attempt.json")):
            raise ValueError(
                "Incomplete preparation has attempts; reconcile without resetting state."
            )
        delta.prepare(folder, ROOT, successor=True)
        for name in SEED_FILES:
            write(folder / "origin" / name, (seed / name).read_text())
        write(folder / "state.json", state)
        write(folder / "worker.json", config)


def health(folder, store, status, reason, **extra):
    boot = Path("/proc/sys/kernel/random/boot_id")
    value = dict(
        schema="monitor-worker-health-v1",
        symbol="DE",
        status=status,
        reason=reason,
        updated_at=stamp(),
        boot_id=boot.read_text().strip() if boot.exists() else None,
        attempts=len(list((folder / "cycles").glob("*/attempt.json"))),
        max_attempts=1,
        hourly_enabled=False,
        execute=False,
        **extra,
    )
    state = folder / "state.json"
    if state.exists():
        value["state_sha256"] = digest(live.compact(read(state)))
    if (folder / "pilot.json").exists():
        pilot = read(folder / "pilot.json")
        results = [read(p) for p in sorted((folder / "cycles").glob("*/result.json"))][1:]
        value.update(
            pilot_id=pilot["pilot_id"],
            starts_at=pilot["starts_at"],
            ends_at=pilot["ends_at"],
            max_attempts=25,
            max_new_attempts=24,
            new_attempts=max(0, value["attempts"] - 1),
            hourly_enabled=status not in ("held", "paused", "pilot_complete"),
            completed_new_cycles=sum(r["status"] == "completed" for r in results),
            cost_estimate_usd=sum(r.get("cost", {}).get("total_estimate_usd", 0) for r in results),
            cost_incomplete=status == "running"
            or len(results) != max(0, value["attempts"] - 1)
            or any("total_estimate_usd" not in r.get("cost", {}) for r in results),
        )
    write(folder / "health.json", value)
    store.put("health/DE.json", value, store.get("health/DE.json")[1])
    print(json.dumps(value, sort_keys=True), flush=True)
    return value


def tick(folder, store, client=None, now=None, broker=True, dispatch=True):
    with (folder / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = read(folder / "worker.json")
        if config["max_attempts"] != 1:
            raise ValueError("This release permits one fresh attempt only.")
        pilot = read(folder / "pilot.json") if (folder / "pilot.json").exists() else None
        if pilot:
            from worker.pilot import validate_plan

            validate_plan(pilot)
            config = dict(config, code=pilot["code"], max_attempts=25)
        for base, field in ((ROOT, "code"), (folder / "origin", "origin")):
            if any(digest((base / n).read_bytes()) != sha for n, sha in config[field].items()):
                raise ValueError("Bound worker source or origin changed.")
        watch = delta.verify(folder)
        attempts = sorted((folder / "cycles").glob("*/attempt.json"))
        if any(not (a.parent / "result.json").exists() for a in attempts):
            return health(folder, store, "held", "interrupted_attempt_requires_review")
        state = live.recover_state(folder, watch)
        expected = read(folder / "origin/state-after.json")
        for attempt in attempts:
            target = attempt.parent
            result = read(target / "result.json")
            if read(target / "state-before.json") != expected or result[
                "state_before_sha256"
            ] != digest(live.compact(expected)):
                raise ValueError("Cycle did not consume the preceding accepted state.")
            expected = result.get("next_state", expected)
            if result.get("requires_review"):
                expected = dict(expected, requires_review=result["requires_review"])
            if result["status"] == "completed":
                after = target / "state-after.json"
                if after.exists() and read(after) != expected:
                    raise ValueError("Saved accepted state differs from the terminal result.")
                write(after, expected)
        if state != expected:
            raise ValueError("Worker state differs from its recorded chain.")
        # Re-publish saved terminal records even after a previous success. Content-addressed
        # records and conditional pointers make a reboot/publication replay harmless.
        for attempt in attempts:
            target = attempt.parent
            record = load_cycle(target, "DE")
            key = publish(store, record)
            write(target / "publication.json", dict(key=key, verified_at=stamp()))
        if state.get("requires_review") or any(
            read(a.parent / "result.json")["status"] != "completed" for a in attempts
        ):
            return health(folder, store, "held", "failed_attempt_requires_review")
        latest = {}
        if attempts:
            latest = attempts[-1].parent
            latest = dict(
                last_cycle=latest.name,
                information_cutoff=read(latest / "result.json")["as_of"],
                broker_status=read(latest / "ibkr-context.json")["status"],
                publication_key=read(latest / "publication.json")["key"],
            )
        cutoff = now or stamp()
        if pilot and (folder / "PAUSED").exists():
            return health(folder, store, "paused", "operator_pause", **latest)
        if len(attempts) >= config["max_attempts"] or (
            pilot and live.instant(cutoff) >= live.instant(pilot["ends_at"])
        ):
            return health(
                folder,
                store,
                "pilot_complete" if pilot else "rehearsal_complete",
                "pilot_limit_reached" if pilot else "one_attempt_limit_reached",
                **latest,
            )
        if pilot and live.instant(cutoff) < live.instant(pilot["starts_at"]):
            return health(folder, store, "pilot_ready", "waiting_for_first_hour", **latest)
        if (
            not live.instant(watch["issued_at"])
            <= live.instant(cutoff)
            < live.instant(watch["valid_until"])
        ):
            return health(folder, store, "held", "baseline_not_active")
        cycle = "hour-" + live.instant(cutoff).astimezone(timezone.utc).strftime("%Y%m%dT%H0000Z")
        target = folder / "cycles" / cycle
        if pilot and (not dispatch or (target / "attempt.json").exists()):
            next_due = live.instant(cutoff).replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
            return health(
                folder,
                store,
                "waiting",
                "hour_already_finished",
                next_due=next_due.isoformat(),
                **latest,
            )
        before = target / "state-before.json"
        if before.exists() and read(before) != state:
            raise ValueError("Pre-dispatch checkpoint changed.")
        delta.request_for(watch, state, cutoff, cycle, {"status": "not_collected"})
        if client is None:
            from worker.runtime import credentials

            credentials()
        write(before, state)
        health(folder, store, "running", "fresh_cycle", last_cycle=cycle)
        live.run_cycle(
            folder,
            cycle,
            client=client,
            broker=broker,
            now=now,
            verifier=delta.verify,
            requester=delta.request_for,
            validator=delta.validate_observation,
            advancer=delta.advance,
            renderer=delta.render,
        )
    return tick(folder, store, client=client, now=now, broker=broker, dispatch=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "tick"))
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--store", default=os.environ.get("REPORT_STORE"))
    args = parser.parse_args()
    if args.command == "prepare":
        if args.seed is None:
            parser.error("prepare requires --seed")
        prepare(args.session, args.seed)
        return
    if not args.store:
        parser.error("tick requires --store or REPORT_STORE")
    store = Store(args.store)
    try:
        result = tick(args.session, store)
        if result["status"] == "held":
            raise SystemExit(2)
    except (ValueError, KeyError) as error:
        health(
            args.session,
            store,
            "held",
            "state_or_source_validation",
            error_type=type(error).__name__,
        )
        raise SystemExit(2) from None
    except Exception as error:
        # Retry service delivery, never clear an attempt or repeat a model submission.
        try:
            health(
                args.session,
                store,
                "retry_pending",
                "infrastructure_or_publication",
                error_type=type(error).__name__,
            )
        except Exception:
            pass
        print("Worker infrastructure/publication unavailable: " + type(error).__name__, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
