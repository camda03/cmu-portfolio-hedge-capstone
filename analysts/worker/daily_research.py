"""One fresh assessment per subject by the Equity and Index Research Team, with a frozen read-only portfolio packet."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime
from functools import partial
import fcntl
import os
from pathlib import Path
import sys
import time

from google.api_core.exceptions import PreconditionFailed
from dashboard.app import Store, active_report, encode, portfolio_research
from monitor import digest, read, stamp, write
from securities import ENABLED
from worker import fleet, reassessment as research, research_usage

DASHBOARD = os.environ.get("RESEARCH_DASHBOARD_URL", "").rstrip("/")
PARALLEL_SUBJECTS = 2


# Create the dated storage record, accepting a repeat only when its contents match.
def publish_once(store, key, value):
    try:
        store.put(key, value, 0)  # Generation zero means the object must be new.
    except PreconditionFailed:
        if store.get(key)[0] != value:
            raise ValueError("Different content already exists at " + key)


# Freeze all seven accepted reports into one packet and return its storage receipt.
def publish_packet(root, staged, store, plan):
    """Freeze the seven accepted reports together so a later run cannot change one member of this batch."""
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
                or forecast["model_allocation"] != research.MODELS
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
                research_status={
                    "label": "Daily assessment published by the Equity and Index Research Team"
                },
            )
        research.immutable(path, packet)
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
        dashboard_url=DASHBOARD + "/api/portfolio-research/test/" + plan["run_id"],
    )


# Combine each model call's receipt and price estimate into the daily cost record.
def accounting(root):
    calls, errors = {}, {}
    for symbol in ENABLED:
        for folder in sorted((root / symbol / "baseline/native").glob("*/")):
            try:
                if not (folder / "stage-attempts").exists() and not list(
                    folder.glob("*/cycles/*/attempt.json")
                ):
                    continue
                for name, row in research_usage.task(folder)["calls"].items():
                    calls[symbol + "/" + folder.name + "/" + name] = row
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors[symbol] = type(error).__name__
    ids = [r["response_id"] for r in calls.values() if r.get("response_id")]
    if len(set(ids)) != len(ids):
        errors["response_ids"] = "Duplicate receipt identity. Totals require reconciliation."
    prices = [r["estimated_usd"] for r in calls.values() if r.get("estimated_usd") is not None]
    amounts = [
        v
        for r in calls.values()
        for v in (r["token_estimate_usd"], r["search_estimate_usd"])
        if v is not None
    ]
    return dict(
        calls=calls,
        receipts=len(calls),
        priced=len(prices),
        errors=errors,
        known_components_usd=round(sum(amounts), 6) if amounts and not errors else None,
        estimated_usd=round(sum(prices), 6)
        if prices and not errors and len(prices) == len(calls)
        else None,
        trial_invoiced_usd=None,
        billing_status="unreconciled",
        standard_rates_per_million=research_usage.STANDARD,
        web_search_usd=research_usage.WEB_SEARCH_USD,
    )


# Summarize a symbol's current research stage from its saved calls and results.
def stage(root, symbol):
    job = root / symbol / "baseline"
    log = job / "native.log"
    if not log.exists():
        return "starting" if (job / "attempt.json").exists() else "waiting"
    pending = []
    for path in job.glob("native/*/stage-attempts/*/*.json"):
        if read(path).get("status") == "started":
            pending.append(path.parent.name)
    for path in job.glob("native/*/*/cycles/*/attempt.json"):
        if not (path.parent / "result.json").exists():
            attempt = read(path)
            status = (attempt.get("execution") or {}).get("provider_status") or attempt.get(
                "phase", "starting"
            )
            pending.append(path.parents[2].name + " " + status)
    if pending:
        return ", ".join(sorted(pending))
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 8192))
        lines = stream.read().decode(errors="replace").splitlines()
    # Emit only operational messages. Keep prompts, financial research and broker diagnostics off the console.
    prefixes = tuple(name + ": " for name in research.STAGES) + (
        "Calling ",
        "Provider status: ",
        "Cycle ",
        symbol + ": ",
    )
    return next(
        (line.split(";")[0][:170] for line in reversed(lines) if line.startswith(prefixes)),
        "collecting inputs",
    )


# Archive the failed price check before allowing another attempt for the same date.
def price_hold(root, session, store):
    """Preserve the original pre-model failure before a new attempt for the same date."""
    status = read(root / "status.json")
    costs = read(root / "costs.json")
    subjects = status["subjects"]
    if (
        status["session_date"] != session
        or status["status"] != "held"
        or status["accepted"] != 0
        or status["costs"]["receipts"] != 0
        or costs["receipts"] != 0
        or costs["calls"]
        or set(subjects) != set(ENABLED)
        or any(
            (
                subjects[s].get("phase") != "price_input"
                and subjects[s].get("last_stage") != s + ": requesting IBKR daily history"
            )
            or subjects[s]["status"] != "held"
            or (root / s / "baseline/native").exists()
            or (root / s / "baseline/completed.json").exists()
            for s in ENABLED
        )
    ):
        raise ValueError(
            "Price recovery only applies to a batch that stopped before model dispatch."
        )
    prefix = "daily-research/" + session + "/attempts/" + status["run_id"] + "/"
    publish_once(store, prefix + "status.json", status)
    publish_once(store, prefix + "costs.json", costs)
    return status["run_id"]


# Coordinate the dated research batch and publish its status, reports, and costs.
def run(root, session, revision, store, analyst=None, interval=30, recover_prices=False):
    if research.live.MODEL_PROFILE != "6.2" or date.fromisoformat(session).isoformat() != session:
        raise ValueError("A New York session date and the approved 6.2 model profile are required.")
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / ".runner.lock").open("a")
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = stamp()
        prefix = "daily-research/" + session + "/"
        previous = price_hold(root.parent, session, store) if recover_prices else None
        plan_file = root / "plan.json"
        if not plan_file.exists():
            initial = dict(
                session_date=session,
                source_revision=revision,
                started_at=started,
                run_id="securities-" + datetime.fromisoformat(started).strftime("%Y%m%dT%H%M%SZ"),
                symbols=list(ENABLED),
                models=research.MODELS,
                discovery_model=research.delta.MODEL,
                hourly_monitoring=False,
            )
            if previous:
                initial.update(
                    previous_run_id=previous,
                    plan_key=prefix + "attempts/" + initial["run_id"] + "/plan.json",
                )
            research.immutable(plan_file, initial)
        plan = read(plan_file)
        if (session, revision, list(ENABLED), research.MODELS) != (
            plan["session_date"],
            plan["source_revision"],
            plan["symbols"],
            plan["models"],
        ):
            raise ValueError("Existing dates, sources or models differ from the requested batch.")
        staged = Store(str(root / "staged"))
        published, generation = store.get(prefix + "status.json")
        if (
            published
            and published["run_id"] != plan["run_id"]
            and (published["run_id"] != previous or published != read(root.parent / "status.json"))
        ):
            raise ValueError("A different attempt already owns this date.")
        state = dict(
            plan,
            status="running",
            subjects={s: dict(status="waiting") for s in ENABLED},
            planned_generations=9 * len(ENABLED),
            execute=False,
        )

        # Save the latest progress locally and publish it against the last storage generation.
        def checkpoint(first=False):
            nonlocal generation
            state["updated_at"] = stamp()
            write(root / "status.json", state)
            print(
                state["updated_at"],
                state["status"],
                " | ".join(
                    s + " " + v.get("detail", v["status"]) for s, v in state["subjects"].items()
                ),
                flush=True,
            )
            try:
                if not first:
                    latest, generation = store.get(prefix + "status.json")
                    if latest and latest["run_id"] != plan["run_id"]:
                        raise ValueError("Another research attempt owns the dated receipt.")
                store.put(prefix + "status.json", state, generation)
            except (PreconditionFailed, ValueError):
                raise
            except Exception as error:
                print("Status upload failed:", type(error).__name__, flush=True)
                if first or state["status"] != "running":
                    raise

        # Verify the destination is writable before issuing paid calls.
        publish_once(store, plan.get("plan_key", prefix + "plan.json"), plan)
        checkpoint(first=True)
        build = partial(
            fleet.build_one,
            root,
            store=staged,
            analyst=analyst or partial(research.native, daily=True),
            for_monitor=False,
        )
        with ThreadPoolExecutor(max_workers=PARALLEL_SUBJECTS) as pool:
            jobs = {pool.submit(build, s): s for s in ENABLED}
            while jobs:
                done, _ = wait(jobs, timeout=interval, return_when=FIRST_COMPLETED)
                for task in done:
                    symbol = jobs.pop(task)
                    try:
                        task.result()
                        state["subjects"][symbol] = dict(status="accepted")
                    except Exception as error:
                        state["subjects"][symbol] = dict(
                            status="held",
                            detail=f"{type(error).__name__}: {error}"[:320],
                            last_stage=stage(root, symbol),
                        )
                        if isinstance(error, research.PriceCollectionError):
                            state["subjects"][symbol]["phase"] = "price_input"
                for task, symbol in jobs.items():
                    if task.running():
                        state["subjects"][symbol] = dict(
                            status="running", detail=stage(root, symbol)
                        )
                checkpoint()
        # Even incomplete batches retain the individual successes and every obtainable cost receipt.
        ledger = accounting(root)
        write(root / "costs.json", ledger)
        for n in range(3):
            try:
                store.put(prefix + "costs.json", ledger, store.get(prefix + "costs.json")[1])
                break
            except Exception as error:
                if n == 2:
                    state["cost_publication_error"] = type(error).__name__
                else:
                    time.sleep(2**n)
        state["costs"] = {
            k: ledger[k]
            for k in (
                "receipts",
                "priced",
                "known_components_usd",
                "estimated_usd",
                "billing_status",
            )
        }
        state["accepted"] = sum(v["status"] == "accepted" for v in state["subjects"].values())
        state["status"] = "held"
        if state["accepted"] == len(ENABLED):
            for n in range(3):
                try:
                    state.update(publish_packet(root, staged, store, plan))
                    state["status"] = "completed"
                    state.pop("publication_error", None)
                    break
                except Exception as error:
                    state["publication_error"] = f"{type(error).__name__}: {error}"[:320]
                    if n < 2:
                        time.sleep(2**n)
        checkpoint()
        return state


# Recover publication of an existing daily batch from its saved results.
def republish(root, session, revision, store):
    # No model-capable functions are reachable from this recovery path.
    with (root / ".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = read(root / "plan.json")
        if (plan["session_date"], plan["source_revision"]) != (session, revision):
            raise ValueError("The saved run does not match the requested date and revision.")
        state = read(root / "status.json")
        if not all(state["subjects"][s]["status"] == "accepted" for s in ENABLED):
            raise ValueError("All seven saved reports must be accepted before publication.")
        ledger = read(root / "costs.json")
        prefix = "daily-research/" + session + "/"
        store.put(prefix + "costs.json", ledger, store.get(prefix + "costs.json")[1])
        state.update(
            publish_packet(root, Store(str(root / "staged")), store, plan),
            accepted=len(ENABLED),
            status="completed",
            updated_at=stamp(),
            costs={
                k: ledger[k]
                for k in (
                    "receipts",
                    "priced",
                    "known_components_usd",
                    "estimated_usd",
                    "billing_status",
                )
            },
        )
        state.pop("publication_error", None)
        state.pop("cost_publication_error", None)
        write(root / "status.json", state)
        store.put(prefix + "status.json", state, store.get(prefix + "status.json")[1])
        return state


if __name__ == "__main__":
    mode = sys.argv[1] if sys.argv[1] in ("--publish", "--recover-prices") else None
    folder, session_date, source_revision = sys.argv[2:] if mode else sys.argv[1:]
    args = (Path(folder), session_date, source_revision, Store(os.environ["REPORT_STORE"]))
    outcome = (
        republish(*args)
        if mode == "--publish"
        else run(*args, recover_prices=mode == "--recover-prices")
    )
    sys.exit(0 if outcome["status"] == "completed" else 1)
