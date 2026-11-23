"""Bounded paired Luna prompt comparison; synthetic evidence, shadow actions only."""

import argparse
from datetime import date, datetime, timezone
import fcntl
import hashlib
from html import escape
from importlib.metadata import version
import json
import os
from pathlib import Path
import statistics
import sys
import tarfile
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parent
MODELS = {"baseline": "gpt-6-luna", "coached": "gpt-6-luna"}
PROMPTS = {"baseline": "monitor_prompt_baseline.md", "coached": "monitor_prompt.md"}
# USD / million tokens: input, cache read, 30m write, 12h write, output.
# Standard short-context rates checked 2026-11-09: https://developers.openai.com/api/docs/pricing
RATES = {arm: (0.10, 0.01, 0.125, 0.25, 0.50) for arm in MODELS}
SETTINGS = dict(
    reasoning={"effort": "low"},
    max_output_tokens=2000,
    service_tier="default",
    store=False,
    tools=[],
)
FILES = (
    "monitor.py",
    "monitor.sh",
    "monitor_prompt.md",
    "monitor_prompt_baseline.md",
    "fixtures/monitor/watchlists.json",
    "fixtures/monitor/packets.json",
    "fixtures/monitor/labels.json",
    "fixtures/monitor/sources.json",
)
BUDGET_USD, MAX_REQUEST_BYTES, TIMEOUT = 0.20, 24000, 180


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["quiet", "verify", "reassess", "coverage_gap"]
    condition_ids: list[str]
    evidence_ids: list[str]
    coverage_gaps: list[str]
    reason: str
    verification_question: str


# Serialize a monitor record as consistently ordered, indented JSON.
def dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)


# Return the SHA-256 fingerprint of the supplied text or bytes.
def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


# Save a checkpoint atomically so readers see a complete record.
def write(path, value):
    """Commit a checkpoint before any following external request."""
    directories = {path.parent}
    ancestor = path.parent
    while not ancestor.exists():
        directories.add(ancestor.parent)
        ancestor = ancestor.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("w") as stream:
        stream.write(dump(value) if not isinstance(value, str) else value)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


# Load a monitor record from a JSON file.
def read(path):
    return json.loads(path.read_text())


# Return the current UTC time with its offset for the audit record.
def stamp():
    return datetime.now(timezone.utc).isoformat()


# Check whether the evidence covers the full requested monitoring window.
def has_gap(packet):
    coverage = packet["coverage"]
    through = coverage.get("checked_through")
    return bool(
        coverage["status"] != "complete"
        or coverage["gaps"]
        or not through
        or datetime.fromisoformat(through) < datetime.fromisoformat(packet["as_of"])
    )


# Build the selected monitor's model request from its watchlist, evidence, and instructions.
def request_for(watchlist, packet, prompt, arm):
    # Explicit fields prevent evaluator labels, split names and other arms leaking in.
    payload = {
        "forecast": watchlist,
        **{
            k: packet[k]
            for k in (
                "evidence_mode",
                "since",
                "as_of",
                "coverage",
                "seen_event_ids",
                "prior_events",
                "events",
            )
        },
    }
    return dict(
        model=MODELS[arm],
        instructions=prompt,
        input=dump(payload),
        **SETTINGS,
        text={
            "format": {
                "type": "json_schema",
                "name": "forecast_monitor",
                "strict": True,
                "schema": Decision.model_json_schema(),
            }
        },
    )


# Estimate an upper bound on the cost of the proposed model request.
def reserve(request, arm):
    size = len(dump(request).encode())
    if size > MAX_REQUEST_BYTES:
        raise ValueError("Packet exceeds the fixed request-size limit; no request made.")
    # Deliberately conservative local reservation: bytes + overhead as token proxy,
    # highest short-context input/cache-write rate, and full output allowance.
    return (
        (size + 1024) * max(RATES[arm][:-1]) + SETTINGS["max_output_tokens"] * RATES[arm][-1]
    ) / 1_000_000


# Estimate the call's cost from its reported token usage and applicable rates.
def token_cost(usage, arm, rates=None):
    """Rate-based estimate, not an invoice; reasoning is already inside output_tokens."""
    total, output = usage["input_tokens"], usage["output_tokens"]
    details = usage.get("input_tokens_details") or {}
    cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    durations = details.get("cache_write_tokens_details") or {}
    short = durations.get("ttl_30m_tokens", 0)
    long = durations.get("ttl_12h_tokens", written - short)
    if (
        min(total, output, cached, written, short, long) < 0
        or cached + written > total
        or short + long != written
    ):
        raise ValueError("Inconsistent usage breakdown; retain usage for manual reconciliation.")
    counts = (total - cached - written, cached, short, long, output)
    return (
        sum(n * rate for n, rate in zip(counts, RATES[arm] if rates is None else rates)) / 1_000_000
    )


# Check that the evaluation packets, watchlists, labels, and sources refer to matching cases.
def validate_suite(watches, packets, labels, sources):
    ids = [p["id"] for p in packets]
    if not 1 <= len(packets) <= 12 or len(set(ids)) != len(ids) or set(ids) != set(labels):
        raise ValueError("Require 1..12 unique cases and exactly one separate label per case.")
    for p in packets:
        w = watches[p["symbol"]]
        if not w["synthetic"] or w["symbol"] != p["symbol"] or not p["id"].isalnum():
            raise ValueError(
                "This runner requires a hypothetical forecast and an alphanumeric case ID."
            )
        if p["group"] not in ("regression", "transfer", "coverage_control"):
            raise ValueError("Unknown evaluation group.")
        mode = "synthetic"
        if p["evidence_mode"] != mode:
            raise ValueError("Evidence origin and evaluation group disagree.")
        times = [
            datetime.fromisoformat(t)
            for t in (w["issued_at"], p["since"], p["as_of"], w["valid_until"])
        ]
        if any(t.tzinfo is None for t in times) or times != sorted(times):
            raise ValueError("Fixture dates must be zoned and within the forecast horizon.")
        observations = p["prior_events"] + p["events"]
        event_ids = [e["id"] for e in observations]
        if len(event_ids) != len(set(event_ids)) or p["coverage"]["status"] not in (
            "complete",
            "incomplete",
        ):
            raise ValueError("Invalid event IDs or coverage status.")
        if not set(p["seen_event_ids"]) <= set(event_ids):
            raise ValueError("Seen observations must be supplied, not merely named.")
        through = p["coverage"].get("checked_through")
        if through and (
            datetime.fromisoformat(through).tzinfo is None
            or datetime.fromisoformat(through) > times[2]
        ):
            raise ValueError("Coverage cannot be unzoned or extend beyond the simulated cutoff.")
        for e in observations:
            available = datetime.fromisoformat(e.get("available_at", ""))
            bound = times[1] if e in p["prior_events"] else times[2]
            if available.tzinfo is None or available > bound:
                raise ValueError("Evidence unavailable by its simulated cutoff.")
            if e.get("published_at"):
                published = datetime.fromisoformat(e["published_at"])
                if published.tzinfo is None or published > available:
                    raise ValueError("Publication must precede its zoned availability bound.")
            if e.get("published_on") and date.fromisoformat(e["published_on"]) > available.date():
                raise ValueError("Publication day exceeds the availability bound.")
            if e.get("source_id") and e["source_id"] not in sources:
                raise ValueError("Public replay evidence lacks its frozen source record.")
        label = labels[p["id"]]
        if label["decision"] not in Decision.model_fields["decision"].annotation.__args__:
            raise ValueError("Unknown evaluator decision.")
        if not set(label["evidence_ids"]) <= set(event_ids):
            raise ValueError("Evaluator refers to missing evidence.")


# Build the evaluation requests and total their estimated cost before dispatch.
def make_plan(files):
    watches, packets, labels, sources = [
        json.loads(files["fixtures/monitor/" + n + ".json"])
        for n in ("watchlists", "packets", "labels", "sources")
    ]
    validate_suite(watches, packets, labels, sources)
    plan = []
    for i, p in enumerate(packets):
        # Counterbalance which prompt pays the first-call latency/cache cost.
        for arm in list(MODELS)[:: 1 if i % 2 == 0 else -1]:
            request = request_for(watches[p["symbol"]], p, files[PROMPTS[arm]].decode(), arm)
            plan.append(
                dict(
                    case=p["id"],
                    arm=arm,
                    request_sha256=digest(dump(request)),
                    reserve_usd=reserve(request, arm),
                )
            )
    if len(plan) > 24:
        raise ValueError("Paired comparison is limited to 24 calls.")
    total = sum(p["reserve_usd"] for p in plan)
    if total > BUDGET_USD:
        raise ValueError(
            f"Full pilot reservation ${total:.3f} exceeds ${BUDGET_USD:.2f}; no calls made."
        )
    return plan, total


# Freeze the evaluation inputs and persist the plan for a repeatable monitor trial.
def prepare(root=ROOT):
    files = {name: (root / name).read_bytes() for name in FILES}
    hashes = {name: digest(data) for name, data in files.items()}
    fingerprint = digest(dump(hashes))
    trial = root / "reports" / "monitor-tests" / fingerprint[:20]
    plan, total = make_plan(files)
    if not trial.exists():
        for name, data in files.items():
            target = trial / "frozen" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        write(
            trial / "manifest.json",
            dict(
                created_at=stamp(),
                mode="paired_prompt_shadow",
                fingerprint=fingerprint,
                files=hashes,
                models=MODELS,
                settings=SETTINGS,
                rates=RATES,
                budget_usd=BUDGET_USD,
                reserved_usd=total,
                plan=plan,
                packages={n: version(n) for n in ("openai", "pydantic")},
            ),
        )
    verify(trial)
    write(root / "reports" / "monitor-current.txt", str(trial.resolve()) + "\n")
    return trial


# Recheck the trial's saved inputs against its manifest before continuing.
def verify(trial):
    manifest = read(trial / "manifest.json")
    if digest(dump(manifest["files"])) != manifest["fingerprint"]:
        raise ValueError("Manifest fingerprint mismatch.")
    for name, expected in manifest["files"].items():
        if digest((trial / "frozen" / name).read_bytes()) != expected:
            raise ValueError(f"Frozen input changed: {name}")
    if digest(Path(__file__).read_bytes()) != manifest["files"]["monitor.py"]:
        raise ValueError("Use the same saved runner version for this experiment.")
    plan, total = make_plan({name: (trial / "frozen" / name).read_bytes() for name in FILES})
    if (
        manifest["plan"] != plan
        or manifest["reserved_usd"] != total
        or manifest["models"] != MODELS
        or manifest["settings"] != SETTINGS
        or manifest["budget_usd"] != BUDGET_USD
        or manifest["rates"] != json.loads(dump(RATES))
    ):
        raise ValueError("Plan, settings or budget changed; no requests made.")
    return manifest


# Validate a monitor decision against the watch conditions and evidence supplied for the case.
def checked_decision(text, watchlist, packet):
    value = Decision.model_validate_json(text).model_dump()
    allowed = {c["id"] for c in watchlist["conditions"]} | {"UNLISTED"}
    observations = packet["prior_events"] + packet["events"]
    if not set(value["condition_ids"]) <= allowed or not set(value["evidence_ids"]) <= {
        e["id"] for e in observations
    }:
        raise ValueError("Decision cites unknown condition or event IDs.")
    if not value["reason"].strip() or (
        value["decision"] in ("verify", "reassess")
        and (not value["condition_ids"] or not value["evidence_ids"])
    ):
        raise ValueError("Action lacks its explanation or supporting IDs.")
    if value["decision"] == "quiet" and observations and not value["evidence_ids"]:
        raise ValueError("Quiet on supplied evidence still requires supporting event IDs.")
    if bool(value["verification_question"].strip()) != (value["decision"] == "verify"):
        raise ValueError("Only verify requires a nonempty, specific verification question.")
    return value


# Map the monitor's decision and any coverage gap to a proposed follow-up action.
def action_for(value, packet):
    decision = value["decision"]
    coverage = packet["coverage"]
    if decision == "quiet" and (has_gap(packet) or value["coverage_gaps"]):
        decision = "coverage_gap"  # Preserve the raw model error in the result and scores.
    return dict(
        action={
            "quiet": "keep_forecast",
            "verify": "verify_evidence",
            "reassess": "full_analysis",
            "coverage_gap": "restore_coverage",
        }[decision],
        command=["bash", "run_all.sh", packet["symbol"]] if decision == "reassess" else [],
        execute=False,
        coverage_gaps=list(dict.fromkeys(coverage["gaps"] + value["coverage_gaps"])),
    )


# Write a progress update for the running monitor trial.
def progress(trial, message):
    print(message, flush=True)
    with (trial / "run.log").open("a") as stream:
        stream.write(f"{stamp()} {message}\n")


# Run the planned monitor cases and checkpoint their responses for later scoring.
def run_trial(trial, client):
    manifest = verify(trial)
    frozen = trial / "frozen"
    watches = read(frozen / "fixtures/monitor/watchlists.json")
    packets = {p["id"]: p for p in read(frozen / "fixtures/monitor/packets.json")}
    prompts = {arm: (frozen / name).read_text() for arm, name in PROMPTS.items()}
    committed = 0.0
    for i, item in enumerate(manifest["plan"], 1):
        arm, case = item["arm"], item["case"]
        target = trial / "results" / f"{case}-{arm}.json"
        if target.exists():
            previous = read(target)
            if previous["request_sha256"] != item["request_sha256"] or previous["status"] not in (
                "completed",
                "invalid_output",
            ):
                raise ValueError(
                    f"{case}/{arm}: prior failed or uncertain attempt. Inspect it; no automatic retry."
                )
            committed += max(item["reserve_usd"], previous.get("estimated_usd", 0))
            continue
        p, w = packets[case], watches[packets[case]["symbol"]]
        request = request_for(w, p, prompts[arm], arm)
        if digest(dump(request)) != item["request_sha256"]:
            raise ValueError("Request changed since preparation; no call made.")
        if committed + item["reserve_usd"] > BUDGET_USD:
            raise ValueError(
                "Local cost reservation would exceed the pilot budget; stopped before dispatch."
            )
        result = dict(item, status="attempted", started_at=stamp(), model=MODELS[arm])
        write(target, result)  # Survives a disconnect after dispatch, before response receipt.
        progress(trial, f"{i}/{len(manifest['plan'])} {case} {arm}: starting")
        started = monotonic()
        try:
            response = client.responses.create(**request)
            result.update(
                response_id=response.id,
                response_status=response.status,
                returned_model=response.model,
                service_tier=response.service_tier,
                elapsed_seconds=round(monotonic() - started, 3),
                usage=response.usage.model_dump() if response.usage else None,
                output_text=response.output_text,
            )
            if (
                response.model != MODELS[arm] and not response.model.startswith(MODELS[arm] + "-")
            ) or response.service_tier not in ("default", None):
                raise ValueError(
                    "Unexpected returned model or service tier; preserve response and inspect pricing."
                )
            if response.usage:
                result["estimated_usd"] = token_cost(result["usage"], arm)
            if response.status != "completed" or not response.output_text:
                raise ValueError("Incomplete or refused response; saved usage, no automatic retry.")
            try:
                value = checked_decision(response.output_text, w, p)
                result.update(status="completed", decision=value, handoff=action_for(value, p))
            except ValueError as error:
                result.update(status="invalid_output", validation_error=str(error))
            write(target, result)
            committed += max(item["reserve_usd"], result.get("estimated_usd", 0))
            cost_text = (
                f"${result['estimated_usd']:.5f}"
                if "estimated_usd" in result
                else "unknown (usage absent)"
            )
            progress(
                trial,
                f"{case} {arm}: {result.get('decision', {}).get('decision', result['status'])}; "
                f"{result['elapsed_seconds']:.1f}s; token-cost estimate {cost_text}",
            )
        except Exception as error:
            # Do not print SDK exception text: it may contain request/credential data.
            result.update(
                status="failed",
                error_type=type(error).__name__,
                http_status=getattr(error, "status_code", None),
                elapsed_seconds=round(monotonic() - started, 3),
            )
            write(target, result)
            progress(
                trial, f"{case} {arm}: FAILED ({type(error).__name__}); stopped without retries."
            )
            raise RuntimeError(
                "Pilot stopped. Saved results and usage are available; inspect before retrying."
            ) from None


# Compare the saved trial decisions with their labels and summarize the results.
def summarize(trial):
    manifest = verify(trial)
    packets = read(trial / "frozen/fixtures/monitor/packets.json")
    labels = read(trial / "frozen/fixtures/monitor/labels.json")
    rows, metrics, details_html = [], {}, []
    for arm in MODELS:
        counts = dict(
            attempted=0,
            completed=0,
            correct=0,
            material_cases=0,
            material_detected=0,
            material_reassessed=0,
            silent_material_misses=0,
            false_full_runs=0,
            verification_cases=0,
            verification_requested=0,
            missed_verification=0,
            routine_escalations=0,
            unsafe_quiet=0,
            invalid=0,
            missing_or_failed=0,
        )
        costs, seconds, confusion = [], [], {}
        for p in packets:
            label, target = labels[p["id"]], trial / "results" / f"{p['id']}-{arm}.json"
            result = read(target) if target.exists() else {"status": "not_attempted"}
            actual = result.get("decision", {}).get("decision", result["status"])
            expected = label["decision"]
            counts["attempted"] += target.exists()
            counts["completed"] += result["status"] == "completed"
            counts["correct"] += actual == expected
            counts["material_cases"] += expected == "reassess"
            counts["material_detected"] += expected == "reassess" and actual in (
                "verify",
                "reassess",
            )
            counts["material_reassessed"] += expected == "reassess" and actual == "reassess"
            counts["silent_material_misses"] += expected == "reassess" and actual == "quiet"
            counts["verification_cases"] += expected == "verify"
            counts["verification_requested"] += expected == "verify" and actual == "verify"
            counts["missed_verification"] += expected == "verify" and actual == "quiet"
            counts["false_full_runs"] += expected != "reassess" and actual == "reassess"
            counts["routine_escalations"] += expected == "quiet" and actual in (
                "verify",
                "reassess",
            )
            counts["unsafe_quiet"] += has_gap(p) and actual == "quiet"
            counts["invalid"] += result["status"] == "invalid_output"
            counts["missing_or_failed"] += result["status"] not in ("completed", "invalid_output")
            key = f"{expected} -> {actual}"
            confusion[key] = confusion.get(key, 0) + 1
            if "estimated_usd" in result:
                costs.append(result["estimated_usd"])
            if "elapsed_seconds" in result:
                seconds.append(result["elapsed_seconds"])
            row = dict(
                case=p["id"],
                group=p["group"],
                symbol=p["symbol"],
                category=label["category"],
                arm=arm,
                expected=expected,
                actual=actual,
                result=result,
                evaluator=label,
            )
            rows.append(row)
            details_html.append(
                f"<details><summary>{escape(p['id'])} · {arm} · expected {expected} · "
                f"got {escape(actual)}</summary><pre>{escape(dump(row))}</pre></details>"
            )
        by_group = {}
        for name in sorted({p["group"] for p in packets}):
            group = [r for r in rows if r["arm"] == arm and r["group"] == name]
            by_group[name] = dict(
                total=len(group),
                correct=sum(r["actual"] == r["expected"] for r in group),
                silent_material_misses=sum(
                    r["expected"] == "reassess" and r["actual"] == "quiet" for r in group
                ),
                verification_cases=sum(r["expected"] == "verify" for r in group),
                verification_requested=sum(
                    r["expected"] == "verify" and r["actual"] == "verify" for r in group
                ),
                missed_verification=sum(
                    r["expected"] == "verify" and r["actual"] == "quiet" for r in group
                ),
                false_full_runs=sum(
                    r["expected"] != "reassess" and r["actual"] == "reassess" for r in group
                ),
            )
        metrics[arm] = dict(
            counts,
            total=len(packets),
            estimated_usd=round(sum(costs), 6),
            usage_records=len(costs),
            by_group=by_group,
            median_seconds=statistics.median(seconds) if seconds else None,
            confusion=confusion,
        )
    complete = all(m["completed"] + m["invalid"] == m["total"] for m in metrics.values())
    pairs = []
    for p in packets:
        a, b = [next(r for r in rows if r["case"] == p["id"] and r["arm"] == arm) for arm in MODELS]
        valid = all(r["result"]["status"] == "completed" for r in (a, b))
        pairs.append(
            dict(
                case=p["id"],
                group=p["group"],
                expected=a["expected"],
                baseline=a["actual"],
                coached=b["actual"],
                valid=valid,
                improved=valid and a["actual"] != a["expected"] == b["actual"],
                regressed=valid and a["actual"] == a["expected"] != b["actual"],
            )
        )
    paired = {}
    for group in ("all", *sorted({p["group"] for p in packets})):
        selected = [p for p in pairs if group == "all" or p["group"] == group]
        paired[group] = dict(
            total=len(selected),
            completed=sum(p["valid"] for p in selected),
            improved=sum(p["improved"] for p in selected),
            regressed=sum(p["regressed"] for p in selected),
            both_correct=sum(
                p["valid"] and p["baseline"] == p["coached"] == p["expected"] for p in selected
            ),
            both_wrong=sum(
                p["valid"] and p["baseline"] != p["expected"] and p["coached"] != p["expected"]
                for p in selected
            ),
            action_changes=sum(p["valid"] and p["baseline"] != p["coached"] for p in selected),
        )
    summary = dict(
        status="finished" if complete else "partial",
        mode="paired_prompt_shadow",
        metrics=metrics,
        rows=rows,
        pairs=pairs,
        paired=paired,
        note="Same fixed synthetic input and Luna settings for baseline and coached prompts. "
        "Known regression, new transfer pairs and coverage control are scored separately. "
        "One response per prompt per case is a descriptive comparison, not a significance test or live reliability estimate. "
        "No live collection, broker requests, or forecasts were invoked. Human evidence review is still required.",
    )
    write(trial / "summary.json", summary)
    heading = (
        "# Monitor experiment v3 — " + summary["status"] + "\n\n" + summary["note"] + "\n\n"
        "Counts use raw decisions, before coverage safeguards. Invalid/missing calls are not correct answers.\n\n"
        "| Prompt arm | Valid / cases | Exact actions | Known token estimate | Usage records / attempts | Median seconds |\n"
        "|---|---:|---:|---:|---:|---:|\n"
    )
    for arm, m in metrics.items():
        heading += (
            f"| {arm} | {m['completed']}/{m['total']} | {m['correct']}/{m['total']} | "
            f"{'$' + format(m['estimated_usd'], '.5f') if m['usage_records'] else 'unknown'}"
            f"{' (incomplete)' if m['usage_records'] < m['attempted'] else ''} | {m['usage_records']}/{m['attempted']} | "
            f"{m['median_seconds'] if m['median_seconds'] is not None else 'pending'} |\n"
        )
    heading += "\n| Prompt arm | Material reassessed / cases | Verification requested / cases | Quiet on material | Quiet on verify | False full runs | Unsafe quiet |\n|---|---:|---:|---:|---:|---:|---:|\n"
    for arm, m in metrics.items():
        heading += (
            f"| {arm} | {m['material_reassessed']}/{m['material_cases']} | "
            f"{m['verification_requested']}/{m['verification_cases']} | {m['silent_material_misses']} | "
            f"{m['missed_verification']} | {m['false_full_runs']} | {m['unsafe_quiet']} |\n"
        )
    heading += "\n| Prompt arm | Evidence group | Exact actions / cases | Quiet on material | Quiet on verify | False full runs |\n|---|---|---:|---:|---:|---:|\n"
    for arm, m in metrics.items():
        for name, group in m["by_group"].items():
            heading += (
                f"| {arm} | {name} | {group['correct']}/{group['total']} | "
                f"{group['silent_material_misses']} | {group['missed_verification']} | {group['false_full_runs']} |\n"
            )
    heading += (
        "\nCosts use saved token usage and checked Standard rates, not billing reconciliation. Missing usage is excluded. "
        "A material case marked verify is detected but still awaits confirmation; it is not a correct reassess action. "
        "Inspect rationale, citations, verification questions and every quiet decision. "
        "Finished means all calls were processed, not that the experiment passed.\n"
    )
    heading += "\n## Paired comparison\n\n| Group | Completed pairs | Improved | Regressed | Both correct | Both wrong | Action changed |\n|---|---:|---:|---:|---:|---:|---:|\n"
    for group, v in paired.items():
        heading += (
            f"| {group} | {v['completed']}/{v['total']} | {v['improved']} | {v['regressed']} | "
            f"{v['both_correct']} | {v['both_wrong']} | {v['action_changes']} |\n"
        )
    heading += (
        "\nImproved means only the coached answer matched the frozen expected action; regressed means only the baseline did. "
        "Invalid or incomplete pairs are excluded from these directional counts and remain visible in the per-arm totals. "
        "Counterfactual pairs are related examples, not independent repeat trials. Review reasons as well as actions.\n"
    )
    heading += (
        "\n## Case decisions\n\n| Case | Evidence group | Expected | " + " | ".join(MODELS) + " |\n"
    )
    heading += "|---|---|---|" + "---|" * len(MODELS) + "\n"
    lookup = {(r["case"], r["arm"]): r["actual"] for r in rows}
    for p in packets:
        case = p["id"]
        heading += (
            f"| {case} | {p['group']} | {labels[case]['decision']} | "
            + " | ".join(lookup[case, arm] for arm in MODELS)
            + " |\n"
        )
    write(trial / "summary.md", heading)
    # Markdown renderer is already a dependency of the analyst project. Disable source HTML.
    from markdown_it import MarkdownIt

    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(heading)
    body += "<h2>Individual decisions and evaluator labels</h2>" + "".join(details_html)
    body += "<h2>Frozen synthetic evidence</h2><pre>" + escape(dump(packets)) + "</pre>"
    body += (
        "<h2>Frozen watchlists</h2><pre>"
        + escape((trial / "frozen/fixtures/monitor/watchlists.json").read_text())
        + "</pre>"
    )
    body += (
        "<h2>Source provenance catalog (empty for synthetic fixtures)</h2><pre>"
        + escape((trial / "frozen/fixtures/monitor/sources.json").read_text())
        + "</pre>"
    )
    write(
        trial / "summary.html",
        '<!doctype html><meta charset="utf-8"><title>Monitor experiment v3</title>'
        "<style>body{font:16px system-ui;max-width:1150px;margin:40px auto;padding:0 20px;color:#172434}"
        "table{border-collapse:collapse;width:100%}th,td{padding:10px;border:1px solid #ccd5de;text-align:left}"
        "th{background:#eef3f8}details{margin:14px 0;padding:12px;background:#f3f6f9}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "</style>" + body,
    )
    with tarfile.open(trial / "results.tar.gz", "w:gz") as bundle:
        for path in sorted(trial.rglob("*")):
            if path.is_file() and path.name != "results.tar.gz":
                bundle.add(path, arcname=str(path.relative_to(trial)), recursive=False)
    return summary


# Dispatch the command to prepare, run, or summarize a monitor trial.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("prepare", "run", "summary"), default="run")
    args = parser.parse_args()
    folder = ROOT / "reports/monitor-tests"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another monitoring experiment is active; no duplicate run started.")
        trial = prepare()
        manifest = read(trial / "manifest.json")
        progress(
            trial,
            f"Experiment: {trial}\nAt most {len(manifest['plan'])} requests; reservation "
            f"${manifest['reserved_usd']:.3f} within ${BUDGET_USD:.2f} local estimate budget. All actions are shadow-only.",
        )
        try:
            if args.action == "run":
                if not os.environ.get("OPENAI_API_KEY"):
                    raise ValueError(
                        "Load the existing approved capstone OPENAI_API_KEY in Cloud Shell; no calls made."
                    )
                from openai import OpenAI

                with OpenAI(max_retries=0, timeout=TIMEOUT) as client:
                    run_trial(trial, client)
        finally:
            summary = summarize(trial)
            progress(
                trial,
                f"Status: {summary['status']}\nHTML: {trial}/summary.html\nReview bundle: {trial}/results.tar.gz",
            )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error))
