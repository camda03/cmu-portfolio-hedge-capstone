"""One configured VM test: fresh baselines, independent hourly monitors, bounded returns."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys

from monitor import read, write, stamp, dump
from monitor_handoff import handoff_structure
from analyst_discovery import attach, carry_forward
from securities import ENABLED, SUBJECTS
from dashboard.app import Store
from worker import reassessment as research

CODE = tuple(
    dict.fromkeys(
        (
            *research.CODE,
            "worker/reassessment_requirements.txt",
            "worker/requirements.txt",
            "requirements.txt",
        )
    )
)


def initialize(root, series, release):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root / "plan.json").exists():
            plan = verify(root)
            if (plan["monitoring_pilot_id"], plan["release_sha256"]) != (series, release):
                raise ValueError("An existing test cannot be reset or rebound.")
            return plan
        now = stamp()
        plan = dict(
            schema="seven-security-test-v1",
            test_id="securities-" + research.live.instant(now).strftime("%Y%m%dT%H%M%SZ"),
            created_at=now,
            bootstrap_ends_at=(research.live.instant(now) + timedelta(hours=8)).isoformat(),
            symbols=list(ENABLED),
            model_profile=research.live.MODEL_PROFILE,
            models=research.MODELS,
            monitor_model=research.delta.MODEL,
            baseline_parallelism=1,
            monitor_hours=24,
            max_monitor_attempts_per_symbol=24,
            max_reassessments_per_symbol=1,
            monitoring_pilot_id=series,
            release_sha256=release,
            execute=False,
            code={n: research.digest((research.ROOT / n).read_bytes()) for n in CODE},
        )
        research.immutable(root / "plan.json", plan)
        return plan


def verify(root):
    plan = read(root / "plan.json")
    if (
        plan.get("model_profile", "legacy") != research.live.MODEL_PROFILE
        or (
            "model_profile" in plan
            and (
                plan.get("models") != research.MODELS
                or plan.get("monitor_model") != research.delta.MODEL
            )
        )
        or plan["schema"] != "seven-security-test-v1"
        or plan["symbols"] != list(ENABLED)
        or plan["execute"] is not False
        or plan["baseline_parallelism"] not in (1, 2)
        or plan["monitor_hours"] != 24
        or plan["max_monitor_attempts_per_symbol"] != 24
        or plan["max_reassessments_per_symbol"] != 1
        or set(plan["code"]) != set(CODE)
        or any(
            research.digest((research.ROOT / n).read_bytes()) != h for n, h in plan["code"].items()
        )
    ):
        raise ValueError("Frozen test configuration or implementation changed.")
    return plan


def baseline_source(symbol, output, *, for_monitor=True):
    forecast = read(output / "forecast.json")
    report = (output / "report.md").read_text()
    valid_forecast = forecast["status"] == "estimate" or (
        not for_monitor
        and forecast["status"] == "abstain"
        and bool(str(forecast.get("reason") or "").strip())
    )
    if (
        forecast["symbol"] != symbol
        or not valid_forecast
        or forecast["forecast_mode"] != "operational"
        or forecast["model_allocation"] != research.MODELS
        or not report.startswith("# " + SUBJECTS[symbol] + ":")
        or any(
            not (output / (name + ext)).is_file()
            for name in research.STAGES
            for ext in (".md", ".json")
        )
    ):
        raise ValueError(
            "First baseline must contain the complete operational seven-stage result for this subject."
        )
    handoff = handoff_structure(forecast["monitoring_handoff"], forecast["forecast_id"])
    from analyst_discovery import saved_packet

    packet = saved_packet(output)
    if (
        forecast.get("shared_discovery") != packet
        or packet["scope_source"]
        not in ("baseline_discovery_categories", "baseline_and_closing_discovery")
        or read(output / "monitoring_handoff.json") != handoff
        or read(output / "discovery_dispositions.json") != forecast["discovery_dispositions"]
    ):
        raise ValueError("First baseline is missing its bound discovery review or handoff.")
    attach(forecast, packet)
    if not for_monitor:
        return dict(
            schema="daily-security-baseline-v1",
            symbol=symbol,
            validated_seven_stages=True,
            forecast_id=forecast["forecast_id"],
            report=report,
            execute=False,
        )
    mapped = research.delta.market.forecast_fields(forecast, price_basis="unverified")
    from analyst_discovery import checks_for

    checks = checks_for(symbol, handoff)
    watch = dict(
        schema="monitor-successor-watch-v1",
        symbol=symbol,
        forecast={
            k: v
            for k, v in forecast.items()
            if k not in ("monitoring_handoff", "reassessment_dispositions")
        },
        information_cutoff=forecast["information_cutoff"],
        issued_at=forecast["issued_at"],
        valid_until=mapped["valid_until"],
        handoff=handoff,
        handoff_sha256=research.digest(dump(handoff)),
        conditions=handoff["conditions"],
        checks=checks,
        baseline_reassessment=dict(
            forecast_id=forecast["forecast_id"],
            information_cutoff=forecast["information_cutoff"],
            issued_at=forecast["issued_at"],
            dispositions=[],
        ),
    )
    state = dict(
        forecast_id=forecast["forecast_id"],
        handoff_sha256=watch["handoff_sha256"],
        prior_events=[],
        checkpoints={},
        attention={},
        considered_referrals=[],
        coverage_gaps=handoff["coverage_gaps"],
        last_observation_at=forecast["issued_at"],
        last_completed_cycle_at=forecast["issued_at"],
        collection_floor=forecast["information_cutoff"],
        inherited_discovery_floor=forecast["information_cutoff"],
        last_decision=None,
    )
    carry_forward(watch, state, forecast)
    state["watch_sha256"] = research.sha(watch)
    return dict(
        schema="fresh-security-baseline-v1",
        status="fresh_validated_baseline",
        execute=False,
        validated_seven_stages=True,
        watch=watch,
        state=state,
        events=state["prior_events"],
        report=report,
    )


def build_one(root, symbol, store, analyst=research.native, *, for_monitor=True):
    job = root / symbol / "baseline"
    if not (job / "completed.json").exists():
        if (
            (job / "attempt.json").exists()
            and not (job / "discovery-resume.json").exists()
            and not (job / "analyst-resume.json").exists()
        ):
            raise ValueError("An interrupted baseline requires review; no automatic model retry.")
        if not (job / "attempt.json").exists():
            research.immutable(
                job / "attempt.json", dict(symbol=symbol, started_at=stamp(), automatic_retries=0)
            )
        output = analyst(job, symbol=symbol).resolve()
        research.immutable(
            job / "completed.json",
            dict(
                output=str(output.relative_to(job.resolve())), files=research.evidence_files(output)
            ),
        )
    completed = read(job / "completed.json")
    output = (job / completed["output"]).resolve()
    output.relative_to(job.resolve())
    if research.evidence_files(output) != completed["files"]:
        raise ValueError("Saved baseline evidence changed.")
    source = baseline_source(symbol, output, for_monitor=for_monitor)
    research.immutable(job / "accepted-source.json", source)
    forecast = read(output / "forecast.json")
    record = dict(
        schema="research-reassessment-v1",
        symbol=symbol,
        forecast=forecast,
        report=source["report"],
        execute=False,
        publication_status="validated_candidate",
        cost_note="Usage is retained in each stage and shared discovery; estimates are not an invoice.",
        stages={
            n: dict(text=(output / (n + ".md")).read_text(), response=read(output / (n + ".json")))
            for n in research.STAGES
        },
    )
    key = "reassessments/" + symbol + "/" + research.sha(record) + ".json"
    prior, version = store.get(key)
    if prior is not None and prior != record:
        raise ValueError("Conflicting baseline publication.")
    if prior is None:
        store.put(key, record, version)
    pointer = "reassessments/" + symbol + ".json"
    store.put(
        pointer,
        dict(
            key=key,
            forecast_id=forecast["forecast_id"],
            status="active_for_monitoring",
            execute=False,
        ),
        store.get(pointer)[1],
    )
    research.immutable(job / "published.json", dict(key=key, forecast_id=forecast["forecast_id"]))


def preflight_price(root, symbol):
    folder = root / symbol
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "price-preflight.log").open("a") as log:
        command = "from run import collect_prices; from pathlib import Path; import sys; collect_prices([sys.argv[1]],cache_dir=Path(sys.argv[2]))"
        subprocess.run(
            [
                "timeout",
                "--kill-after=5s",
                "1000s",
                sys.executable,
                "-c",
                command,
                symbol,
                str(folder / "market-data"),
            ],
            cwd=research.ROOT,
            stdout=log,
            stderr=log,
            check=True,
            timeout=1010,
        )


def bootstrap(root, store, analyst=research.native, price_check=preflight_price):
    plan = verify(root)
    with (root / ".bootstrap.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root / "window.json").exists():
            return
        if research.live.instant(stamp()) >= research.live.instant(plan["bootstrap_ends_at"]):
            raise ValueError("Baseline preparation window expired; no new requests allowed.")

        # All seven data permissions are checked before paying for the first analyst run.
        def prices(symbol):
            try:
                price_check(root, symbol)
                return None
            except Exception as e:
                return dict(symbol=symbol, error_type=type(e).__name__)

        with ThreadPoolExecutor(max_workers=2) as pool:
            errors = [e for e in pool.map(prices, plan["symbols"]) if e]
        write(root / "preflight.json", dict(checked_at=stamp(), errors=errors, model_requests=0))
        if errors:
            raise ValueError(
                "Broker-history preflight failed; inspect preflight.json and the named symbol logs. No baselines submitted."
            )

        def build(symbol):
            try:
                if research.live.instant(stamp()) >= research.live.instant(
                    plan["bootstrap_ends_at"]
                ):
                    raise TimeoutError("Preparation window expired")
                build_one(root, symbol, store, analyst)
                return None
            except Exception as e:
                write(
                    root / symbol / "baseline/error.json",
                    dict(checked_at=stamp(), error_type=type(e).__name__),
                )
                return dict(symbol=symbol, error_type=type(e).__name__)

        with ThreadPoolExecutor(max_workers=plan["baseline_parallelism"]) as pool:
            errors = [e for e in pool.map(build, plan["symbols"]) if e]
        write(root / "bootstrap-result.json", dict(checked_at=stamp(), errors=errors))
        if errors:
            raise ValueError(
                "A baseline requires review; completed baselines will not be resubmitted."
            )
        start = research.live.instant(stamp()).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        research.immutable(
            root / "window.json",
            dict(starts_at=start.isoformat(), ends_at=(start + timedelta(hours=24)).isoformat()),
        )
        enroll(root, store)


def enroll(root, store):
    plan, window = verify(root), read(root / "window.json")
    for symbol in plan["symbols"]:
        session = root / symbol / "session"
        source = dict(
            read(root / symbol / "baseline/accepted-source.json"),
            test_starts_at=window["starts_at"],
            monitoring_pilot_id=plan["monitoring_pilot_id"],
        )
        research.immutable(session / "source.json", source)
        research.prepare(session, session / "source.json")
        cache = session / "market-data"
        cache.mkdir(exist_ok=True)
        saved = root / symbol / "market-data" / (symbol + "-close.json")
        if saved.exists() and not (cache / saved.name).exists():
            shutil.copyfile(saved, cache / saved.name)
        if not (session / "health.json").exists():
            research.tick(session, store, dispatch=False)


def tick(root, symbol, store):
    plan = verify(root)
    if symbol not in plan["symbols"]:
        raise ValueError("Subject not enrolled in this test.")
    if not (root / "window.json").exists():
        return
    session = root / symbol / "session"
    if not (session / "plan.json").exists():
        raise ValueError("Enrollment incomplete; resume bootstrap without new research.")
    os.environ["CAPSTONE_REASSESSMENT_LOCK"] = str(root / ".analyst.lock")
    try:
        return research.tick(session, store)
    except BlockingIOError:
        return None  # Another invocation still owns this subject; do not overwrite its health.
    except Exception as error:
        research.health(
            session, store, "held", "work_requires_review", error_type=type(error).__name__
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "bootstrap", "tick"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbol", choices=ENABLED)
    parser.add_argument("--series")
    parser.add_argument("--release")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.root, args.series, args.release)
    else:
        store = Store(os.environ["REPORT_STORE"])
        if args.command == "bootstrap":
            try:
                if (args.root / "window.json").exists():
                    enroll(args.root, store)
                else:
                    from worker.runtime import credentials

                    credentials()
                    bootstrap(args.root, store)
                (args.root / "bootstrap-error.json").unlink(missing_ok=True)
            except Exception as error:
                write(
                    args.root / "bootstrap-error.json",
                    dict(checked_at=stamp(), error_type=type(error).__name__),
                )
                raise
        else:
            tick(args.root, args.symbol, store)
