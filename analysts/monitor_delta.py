"""Fresh Sol triage using an accepted handoff, with an optional returned baseline."""

import argparse
from datetime import timedelta
import fcntl
from html import escape
from importlib.metadata import version
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, quote_from_bytes, unquote_to_bytes, urlencode, urlsplit

from typing import Literal
import monitor_inputs as market
import monitor_live as live
import monitor_scope as scope
from monitor_handoff import continuity_events
from monitor import digest, dump, read, stamp, write

ROOT = Path(__file__).resolve().parent
FIXTURE = "fixtures/monitor-delta/DE-v1"
HANDOFF = "docs/handoff_check_v1_generated_handoff.json"
FORECAST = "fixtures/monitor-real/DE-v1/forecast.json"
PACKET = "fixtures/monitor-real/DE-v1/packet.json"
DATA = ("checks.json", "initial-state.json")
BASE_FILES = (
    "monitor_delta.py",
    "monitor_delta_prompt.md",
    "monitor_live.py",
    "monitor_scope.py",
    "monitor_handoff.py",
    "monitor.py",
    "monitor_inputs.py",
    "price_watch.py",
    "connect_ibkr.sh",
    "market_sessions.py",
    "securities.py",
    "dashboard/securities.json",
)
CYCLES = ("cycle-1", "cycle-2")
MODEL = live.SOL_MODEL
SUCCESSOR = "fixtures/monitor-successor/DE-v1"
RETURN_REVIEW = "docs/monitor_return_completed_review.json"
RETURN_TRIGGER = "fixtures/monitor-return/DE-v1/trigger.json"


class Event(live.Event):
    change: Literal["new", "update", "duplicate"]


class Observation(live.Strict):
    checks: list[live.Check]
    events: list[Event]
    decision: scope.Decision


class ReferralCase(live.Strict):
    novelty: Literal[
        "new_public_information",
        "older_missed_information",
        "mixed",
        "duplicate_or_already_considered",
        "uncertain",
        "none",
    ]
    baseline_quote: str
    incremental_evidence: str
    material_consequence: str
    counterevidence: str
    why_now: str


class ComparedObservation(Observation):
    referral_case: ReferralCase


# Decide whether a reported change justifies reassessment against the existing forecast.
def qualify_referral(value, payload):
    """Missing comparison remains a watch item; it cannot start full research."""
    case, decision = value["referral_case"], value["decision"]
    qualified, reason = False, ""
    if decision["decision"] == "reassess":
        corpus = "\n".join(
            [payload["baseline_report"], payload["handoff"]["baseline_summary"]]
            + [c["baseline"] for c in payload["handoff"]["conditions"]]
        )
        quote = case["baseline_quote"].strip()
        if case["novelty"] in ("duplicate_or_already_considered", "uncertain", "none"):
            reason = "Incremental material evidence is not established."
        elif any(not text.strip() for key, text in case.items() if key != "novelty"):
            reason = "The baseline comparison or materiality explanation is incomplete."
        elif len(quote) < 20 or quote not in corpus:
            reason = (
                "The claimed baseline premise is not an exact passage in the accepted analysis."
            )
        else:
            qualified = True
        if not qualified:
            decision.update(
                decision="continue",
                reason=reason + " Candidate retained for monitoring. " + decision["reason"],
                review_question="",
                watch_for=decision["watch_for"]
                or "Establish a supported material change to the quoted baseline premise.",
            )
    value["referral_qualification"] = dict(
        policy="baseline-comparison-v1", qualified=qualified, reason=reason
    )
    return value


# Load the watch, starting state, and files needed for the chosen monitoring cycle.
def inputs(root, successor=False):
    if successor:
        return successor_inputs(root)
    provenance = read(root / FIXTURE / "provenance.json")
    if set(provenance["files"]) != set(DATA) or not {HANDOFF, FORECAST, PACKET} <= set(
        provenance["dependencies"]
    ):
        raise ValueError(
            "Require the complete fixture and original accepted handoff/forecast/packet."
        )
    files = {FIXTURE + "/" + name: sha for name, sha in provenance["files"].items()}
    files.update(provenance["dependencies"])
    for name, expected in files.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path.read_bytes()) != expected:
            raise ValueError("Delta source changed: " + name)
    handoff, forecast, packet = (read(root / name) for name in (HANDOFF, FORECAST, PACKET))
    checks, seed = (read(root / FIXTURE / name) for name in DATA)
    if (
        handoff["forecast_id"] != forecast["forecast_id"]
        or seed["forecast_id"] != forecast["forecast_id"]
        or seed["handoff_sha256"] != digest((root / HANDOFF).read_bytes())
        or seed["last_observation_at"] != packet["as_of"]
        or seed["checkpoints"]
        or seed["attention"]
    ):
        raise ValueError(
            "Seed identity or coverage claims do not match the accepted historical evidence."
        )
    ids = {c["id"] for c in handoff["conditions"]}
    if (
        not checks
        or len({c["id"] for c in checks}) != len(checks)
        or {i for c in checks for i in c["condition_ids"]} != ids - {"DE-PRICE-01"}
        or not any(not c["condition_ids"] for c in checks)
    ):
        raise ValueError(
            "News groups must cover the investment conditions and retain an unlisted-news sweep."
        )
    for check in checks:
        for url in check["source_urls"]:
            live.web_url(url)
    prior = seed["prior_events"]
    if (
        {e["id"] for e in prior} != {e["id"] for e in packet["events"]}
        or len(prior) != len({e["event_key"] for e in prior})
        or seed["last_decision"]["case_id"] != packet["id"]
    ):
        raise ValueError("Seed must retain only the actual R01 observations and decision.")
    originals = {e["id"]: e for e in packet["events"]}
    for event in prior:
        original = originals[event["id"]]
        expected_text = original["summary"]
        if original.get("timing_note"):
            expected_text += "\nTiming and provenance: " + original["timing_note"]
        if original.get("scheduled_for"):
            expected_text += "\nRecorded scheduled time: " + original["scheduled_for"] + "."
        if original.get("scheduled_date"):
            expected_text += (
                "\nRecorded scheduled date: " + original["scheduled_date"] + "; hour unverified."
            )
        if (
            event["event_key"] != original["event_key"]
            or event["published_at"] != original["published_at"]
            or event["text"] != expected_text
            or event["source_urls"] != [original["url"]]
        ):
            raise ValueError("Historical event was rewritten or given invented timing.")
    mapped = market.forecast_fields(forecast, price_basis="unverified")
    watch = dict(
        schema="monitor-delta-watch-v1",
        symbol=forecast["symbol"],
        forecast=forecast,
        information_cutoff=forecast["information_cutoff"],
        issued_at=forecast["issued_at"],
        valid_until=mapped["valid_until"],
        handoff=handoff,
        handoff_sha256=seed["handoff_sha256"],
        conditions=handoff["conditions"],
        checks=checks,
    )
    return watch, dict(seed, watch_sha256=digest(live.compact(watch))), tuple(files)


# Reconstruct the next monitor's watch and starting state from the accepted handoff.
def successor_inputs(root):
    provenance = read(root / SUCCESSOR / "provenance.json")
    required = {
        "forecast.json",
        "handoff.json",
        "dispositions.json",
        "report.md",
        "checks.json",
        "initial-state.json",
        "prior-state.json",
    }
    if set(provenance["files"]) != required or set(provenance["dependencies"]) != {
        RETURN_REVIEW,
        RETURN_TRIGGER,
        FIXTURE + "/checks.json",
    }:
        raise ValueError("Require the complete returned baseline and accepted predecessor.")
    files = {SUCCESSOR + "/" + n: s for n, s in provenance["files"].items()}
    files.update(provenance["dependencies"])
    for name, expected in files.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path.read_bytes()) != expected:
            raise ValueError("Successor source changed: " + name)
    forecast, handoff, dispositions, checks, seed, prior = (
        read(root / SUCCESSOR / n)
        for n in (
            "forecast.json",
            "handoff.json",
            "dispositions.json",
            "checks.json",
            "initial-state.json",
            "prior-state.json",
        )
    )
    review = read(root / RETURN_REVIEW)
    trigger = read(root / RETURN_TRIGGER)
    native = "output/DE/" + forecast["forecast_id"].split(":")[1] + "/"
    for name, original in [
        ("forecast.json", "forecast.json"),
        ("handoff.json", "monitoring_handoff.json"),
        ("dispositions.json", "reassessment_dispositions.json"),
        ("report.md", "report.md"),
    ]:
        if (
            digest((root / SUCCESSOR / name).read_bytes())
            != review["artifact_hashes"][native + original]
        ):
            raise ValueError("Returned baseline differs from its completed review.")
    if (
        forecast["monitoring_handoff"] != handoff
        or forecast["reassessment_dispositions"] != dispositions
        or prior != trigger["monitor_state"]
        or forecast["monitor_trigger_sha256"] != digest(live.compact(trigger))
        or digest(live.compact(prior))
        != review["audits"]["integrity"]["input_checks"]["monitor_state_sha256"]
        or seed["forecast_id"] != forecast["forecast_id"]
        or handoff["forecast_id"] != forecast["forecast_id"]
        or forecast["predecessor_forecast_id"] != prior["forecast_id"]
        or seed["handoff_sha256"] != digest((root / SUCCESSOR / "handoff.json").read_bytes())
        or seed["collection_floor"] != forecast["information_cutoff"]
    ):
        raise ValueError("Successor lineage, handoff or collection floor changed.")
    inherited = set(prior) - {"forecast_id", "handoff_sha256", "watch_sha256"}
    if set(seed) != inherited | {
        "forecast_id",
        "handoff_sha256",
        "collection_floor",
        "inherited_discovery_floor",
        "considered_referrals",
    }:
        raise ValueError("Successor seed must contain only the explicit reviewed transition.")
    for key in inherited:
        if seed.get(key) != prior.get(key):
            raise ValueError("Successor must preserve historical state: " + key)
    if seed["inherited_discovery_floor"] != trigger["original_forecast"]["information_cutoff"]:
        raise ValueError("Inherited discovery floor cannot erase previous monitoring coverage.")
    considered = seed["considered_referrals"]
    if len(considered) != len(dispositions):
        raise ValueError("Every returned disposition needs a considered-referral record.")
    for record, disposition in zip(considered, dispositions):
        latest = {
            e["id"]
            for e in prior["prior_events"]
            if e["event_key"] in disposition["incorporated_event_keys"]
        }
        versions = latest | {
            v
            for a in prior["attention"].values()
            if set(a.get("event_keys", [])) & set(disposition["incorporated_event_keys"])
            for v in a.get("evidence_versions", [])
        }
        if (
            record["referral_id"] != disposition["referral_id"]
            or record["disposition"] != disposition["disposition"]
            or record["condition_ids"] != disposition["monitor_condition_ids"]
            or record["event_keys"] != disposition["incorporated_event_keys"]
            or record["considered_by_forecast_id"] != forecast["forecast_id"]
            or set(record["evidence_versions"]) != versions
            or record["risk_remains_active"] is not True
        ):
            raise ValueError(
                "Considered referral lost its disposition or latest incorporated version."
            )
    ids = {c["id"] for c in handoff["conditions"]}
    if (
        not checks
        or len({c["id"] for c in checks}) != len(checks)
        or {i for c in checks for i in c["condition_ids"]} != ids - {"DE-PRICE-01"}
        or not any(not c["condition_ids"] for c in checks)
    ):
        raise ValueError(
            "Successor discovery must cover every news condition and an unexpected-news sweep."
        )
    for check in checks:
        for url in check["source_urls"]:
            live.web_url(url)
    forecast = {
        k: v
        for k, v in forecast.items()
        if k not in ("monitoring_handoff", "reassessment_dispositions")
    }
    mapped = market.forecast_fields(forecast, price_basis="unverified")
    watch = dict(
        schema="monitor-successor-watch-v1",
        symbol=forecast["symbol"],
        forecast=forecast,
        information_cutoff=forecast["information_cutoff"],
        issued_at=forecast["issued_at"],
        valid_until=mapped["valid_until"],
        handoff=handoff,
        handoff_sha256=seed["handoff_sha256"],
        conditions=handoff["conditions"],
        checks=checks,
        baseline_reassessment=dict(
            forecast_id=forecast["forecast_id"],
            predecessor_forecast_id=forecast["predecessor_forecast_id"],
            information_cutoff=forecast["information_cutoff"],
            issued_at=forecast["issued_at"],
            dispositions=dispositions,
            original_forecast_sha256=provenance["files"]["forecast.json"],
        ),
    )
    return (
        watch,
        dict(seed, watch_sha256=digest(live.compact(watch))),
        tuple(files) + (SUCCESSOR + "/provenance.json",),
    )


# Evaluate the current broker observation against the watch's price conditions.
def price_check(watch, ibkr, cutoff):
    mapped = market.forecast_fields(watch["forecast"], price_basis="unverified")
    result = dict(
        status="unqualified",
        qualified_price_trigger=False,
        execute=False,
        session=market.session_fields(mapped["reference_at"], cutoff, symbol=watch["symbol"]),
        snapshot_status=ibkr["status"],
        reasons=[
            "No actual mechanical watch or qualified comparison basis is initialized in this test."
        ],
    )
    saved = ibkr if ibkr.get("status") == "received" else ibkr.get("retained_snapshot")
    result.update(
        quote_session=ibkr.get("quote_session"), reused_snapshot=bool(saved and saved is not ibkr)
    )
    if saved:
        try:
            result["snapshot"] = market.qualify_snapshot(
                saved["response"],
                saved["retrieved_at"],
                checked_at=cutoff,
                request=saved["request"],
                symbol=watch["symbol"],
            )
            result["snapshot_retrieved_at"] = saved["retrieved_at"]
        except (KeyError, TypeError, ValueError) as error:
            result["snapshot_error"] = type(error).__name__
    if result["session"]["market_session"] != "REGULAR":
        result["reasons"].append(
            "Outside the regular NYSE session; no fresh regular-session price trigger is claimed."
        )
    result.update(
        retained_reference=dict(
            price=watch["forecast"]["reference_price"],
            session_date=watch["forecast"]["price_date"],
            currency=watch["forecast"]["quote_unit"],
            source=watch["forecast"]["price_source"],
        ),
        market_data_mode="dated_reference"
        if result["session"]["market_session"] != "REGULAR"
        else "regular_session_diagnostic",
        overnight_age_is_feed_failure=False,
        last_relative_to_reference="unknown",
        context_note="Retain dated IBKR context. Normal overnight age alone is not a feed failure. "
        "Transport errors remain separate. A new session requires a new valid observation; "
        "an earlier last cannot establish a move after a later reference close.",
    )
    observed = result.get("snapshot", {}).get("interpreted_last_at")
    if observed:
        result["last_relative_to_reference"] = (
            "before_reference"
            if live.instant(observed) < live.instant(mapped["reference_at"])
            else "at_or_after_reference"
        )
    return result


# Build a news-monitor request that includes prior findings and the current price check.
def request_for(watch, state, cutoff, cycle, ibkr, model=MODEL, *, baseline_report=None):
    if model != MODEL or live.instant(cutoff) < live.instant(
        state.get("last_completed_cycle_at", state["last_observation_at"])
    ):
        raise ValueError("Wrong model or a cycle before the last accepted observation/cutoff.")
    floor = state.get("inherited_discovery_floor", watch["information_cutoff"])
    windows = {
        c["id"]: dict(
            since=max(
                live.instant(floor),
                live.instant(state["checkpoints"].get(c["id"], floor)) - timedelta(hours=2),
            ).isoformat(),
            as_of=cutoff,
        )
        for c in watch["checks"]
    }
    if state.get("collection_floor") and not state.get("baseline_continuation_completed_at"):
        for window in windows.values():
            window["since"] = min(
                live.instant(window["since"]), live.instant(state["collection_floor"])
            ).isoformat()
    checks = [
        {
            **{k: v for k, v in c.items() if k != "source_urls"},
            "navigation_targets": c["source_urls"],
        }
        for c in watch["checks"]
    ]
    prior = [
        {
            **{k: v for k, v in e.items() if k != "source_urls"},
            "navigation_targets": e["source_urls"],
        }
        for e in state["prior_events"]
    ]
    payload = dict(
        handoff=watch["handoff"],
        forecast=watch["forecast"],
        checks=checks,
        requested_windows=windows,
        as_of=cutoff,
        as_of_new_york=live.instant(cutoff)
        .astimezone(live.ZoneInfo("America/New_York"))
        .isoformat(),
        case_id=cycle,
        event_id_prefix=cycle + "-",
        prior_events=prior,
        prior_attention=state["attention"],
        last_decision=state.get("last_decision"),
        prior_coverage_gaps=state.get("coverage_gaps", []),
        last_observation_at=state["last_observation_at"],
        state_before_sha256=digest(live.compact(state)),
        handoff_sha256=watch["handoff_sha256"],
        ibkr=ibkr,
        price_check=price_check(watch, ibkr, cutoff),
    )
    if state.get("unverified_findings"):
        payload["unverified_leads"] = state["unverified_findings"]
    if watch.get("baseline_reassessment"):
        payload.update(
            baseline_reassessment=watch["baseline_reassessment"],
            considered_referrals=state["considered_referrals"],
            baseline_note="The Big Seven already considered these episodes and issued this new baseline. "
            "Prior attention and decision are retained history. Follow the returned dispositions and "
            "remaining watches; unchanged incorporated facts do not justify another referral. "
            "Research questions remain assigned to their owners. Forecast JSON is projected without "
            "duplicating its separately supplied handoff and dispositions. Original files are frozen.",
        )
    if baseline_report is not None:
        if not isinstance(baseline_report, str) or not baseline_report.strip():
            raise ValueError("Require the full accepted analyst report for referral comparison.")
        payload.update(baseline_report=baseline_report, referral_policy="baseline-comparison-v1")
    schema = (
        ComparedObservation if baseline_report is not None else Observation
    ).model_json_schema()
    properties = schema["$defs"]["Decision"]["properties"]
    properties["case_id"]["enum"] = [cycle]
    properties["condition_ids"]["items"]["enum"] = [c["id"] for c in watch["conditions"]] + [
        "UNLISTED"
    ]
    schema["$defs"]["Check"]["properties"]["id"]["enum"] = [c["id"] for c in checks]
    request = dict(
        live.SETTINGS,
        model=model,
        instructions=(ROOT / "monitor_delta_prompt.md").read_text(),
        input=live.compact(payload),
        text={"format": dict(type="json_schema", name="monitor_delta", strict=True, schema=schema)},
    )
    return request, payload


# Collect the source records and search calls reported by the provider.
def source_trace(raw):
    urls, calls = live.trace_sources(raw)
    records = {url: ["completed_web_action"] for url in urls}
    for item in raw.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            for annotation in part.get("annotations", []):
                if annotation.get("type") == "url_citation":
                    url = live.web_url(annotation["url"])
                    records.setdefault(url, []).append("provider_final_url_citation")
    return records, calls


# Build a URL comparison key for matching citations to their source traces.
def source_locator(url):
    """Build a comparison key with urllib; retain the provider's original URL.

    Encode each path segment separately, so %2F never becomes a separator.
    Retain domains, fragments, query order and duplicate parameters. This is
    a citation-spelling policy, not proof that a server serves identical content.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None  # A malformed model citation is an unverified lead.
    if re.search(r"%(?![0-9a-fA-F]{2})", parts.path):
        return None
    parts = parts._replace(
        path="/".join(
            quote_from_bytes(unquote_to_bytes(segment), safe="")
            for segment in parts.path.split("/")
        )
    )
    if parts.query and all("=" in field for field in parts.query.split("&")):
        try:
            if not re.search(r"%(?![0-9a-fA-F]{2})", parts.query):
                parts = parts._replace(
                    query=urlencode(parse_qsl(parts.query, keep_blank_values=True, errors="strict"))
                )
        except ValueError:
            pass  # Malformed escaping cannot establish equivalence.
    if parts.scheme == "https" and parts.netloc == "apnews.com":
        article = re.fullmatch(r"/article/(?:[a-z0-9-]+-)?([0-9a-f]{32})", parts.path)
        if article:
            return parts._replace(path="/article/" + article[1]).geturl()
    return parts.geturl()


# Match reported sources to traced URLs and retain unsupported leads for follow-up.
def match_sources(value, traces):
    """Shared attribution gate: exact traced URLs or preserved, unverified leads."""
    normalizations, rejections = [], []
    for item in value["checks"] + value["events"]:
        for index, url in enumerate(item["source_urls"]):
            if url not in traces:
                locator = source_locator(url)
                matches = [
                    u for u in traces if locator is not None and source_locator(u) == locator
                ]
                if len(matches) == 1:
                    item["source_urls"][index] = matches[0]
                    normalizations.append(
                        dict(item_id=item["id"], reported_url=url, provider_url=matches[0])
                    )
    for check in value["checks"]:
        missing = sorted(set(check["source_urls"]) - set(traces))
        if missing or (check["status"] == "checked" and not check["source_urls"]):
            rejections.append(
                dict(
                    check_id=check["id"],
                    rejected_urls=missing,
                    reason="Coverage needs current provider-attributed sources.",
                )
            )
            check["source_urls"] = [u for u in check["source_urls"] if u in traces]
            if check["status"] == "checked":
                check["status"] = "partial"
    unverified = []
    for event in value["events"]:
        missing = sorted(set(event["source_urls"]) - set(traces))
        if missing or not event["source_urls"]:
            unverified.append(event)
            rejections.append(
                dict(
                    event_id=event["id"],
                    rejected_urls=missing,
                    reason="Unverified lead only; analysts must independently establish support.",
                )
            )
    return normalizations, rejections, unverified


# Check the new observation's evidence and determine whether it warrants a referral.
def validate_observation(raw, text, watch, state, payload, model=MODEL):
    if (
        raw.get("status") != "completed"
        or not (raw.get("model") == model or (raw.get("model") or "").startswith(model + "-"))
        or raw.get("service_tier") not in (None, "default")
    ):
        raise ValueError("Require a completed response from the requested model and service tier.")
    if any(
        x.get("type", "").endswith("_call") and x["type"] != "web_search_call"
        for x in raw.get("output", [])
    ):
        raise ValueError("Unexpected tool type in the news-only monitor.")
    contract = (
        ComparedObservation
        if payload.get("referral_policy") == "baseline-comparison-v1"
        else Observation
    )
    value = contract.model_validate_json(text).model_dump()
    traces, calls = source_trace(raw)
    normalizations, rejections, unverified = match_sources(value, traces)
    expected = {c["id"] for c in watch["checks"]}
    if len(value["checks"]) != len(expected) or {c["id"] for c in value["checks"]} != expected:
        raise ValueError("Every requested news group needs an explicit coverage status.")
    old = continuity_events(watch.get("forecast", {}), state["prior_events"])
    ids = {e["id"] for e in old.values()}
    keys = set()
    for event in value["events"]:
        previous = old.get(event["event_key"])
        if (
            not event["id"].startswith(payload["event_id_prefix"])
            or event["id"] in ids
            or not event["event_key"].strip()
            or event["event_key"] in keys
            or not event["text"].strip()
            or event["previous_event_id"] != (previous["id"] if previous else None)
            or (event["change"] == "new") != (previous is None)
        ):
            raise ValueError(
                "New evidence has an invalid identity or change classification: " + event["id"]
            )
        if event["published_at"] is not None and live.instant(event["published_at"]) > live.instant(
            payload["as_of"]
        ):
            raise ValueError("Evidence publication is after the cycle cutoff.")
        ids.add(event["id"])
        keys.add(event["event_key"])
    # Check the original proposal before excluding leads, so bad IDs cannot be hidden.
    scope.check_decision(
        value["decision"],
        watch,
        dict(id=payload["case_id"], prior_events=state["prior_events"], events=value["events"]),
    )
    if unverified:
        rejected = {e["id"] for e in unverified}
        value["source_review"] = dict(
            unverified_findings=unverified, proposed_decision=read_copy(value["decision"])
        )
        value["events"] = [e for e in value["events"] if e["id"] not in rejected]
        if rejected.intersection(value["decision"]["evidence_ids"]):
            value["decision"].update(
                decision="continue",
                review_question="",
                evidence_ids=[i for i in value["decision"]["evidence_ids"] if i not in rejected],
                reason="Source verification is incomplete; the saved proposal is not accepted. Monitoring continues.",
                watch_for="Independently verify these leads: "
                + ", ".join(e["event_key"] for e in unverified),
            )
    unresolved = {e["event_key"]: e for e in state.get("unverified_findings", [])}
    unresolved.update({e["event_key"]: e for e in unverified})
    for event in value["events"]:
        unresolved.pop(event["event_key"], None)
    if unresolved:
        # A finding has no reliable check-group mapping; retain every search window.
        for check in value["checks"]:
            if check["status"] == "checked":
                check["status"] = "partial"
    gaps = [
        c["id"] + ": " + c["status"] + " — " + c["note"]
        for c in value["checks"]
        if c["status"] != "checked"
    ]
    if payload["ibkr"]["status"] != "market_closed":
        gaps.append("IBKR: " + payload["ibkr"]["status"])
    elif not payload["ibkr"].get("retained_snapshot"):
        gaps.append(
            "Configured quote feed closed; no saved snapshot. Dated forecast reference retained."
        )
    gaps.append("Actual price trigger remains unqualified.")
    gaps += [
        "Source verification pending for " + e["id"] + ": " + e["text"] for e in unresolved.values()
    ]
    packet = dict(
        id=payload["case_id"],
        as_of=payload["as_of"],
        prior_events=state["prior_events"],
        events=value["events"],
        coverage=dict(
            status="incomplete",
            checked_through=payload["as_of"],
            gaps=gaps,
            baseline_research_gaps=watch["handoff"]["coverage_gaps"],
            source_rejections=rejections,
            source_trace=traces,
            attribution_note="A provider-attributed URL is not proof of an individual open or of claim truth.",
        ),
    )
    if normalizations:
        packet["coverage"]["source_url_normalizations"] = normalizations
    if unresolved:
        packet["coverage"]["unverified_findings"] = list(unresolved.values())
    if payload.get("referral_policy") == "baseline-comparison-v1":
        qualify_referral(value, payload)
    decision = scope.check_decision(value["decision"], watch, packet)
    if decision["decision"] == "continue" and not decision["watch_for"].strip():
        raise ValueError("Continue must retain an observable watch condition.")
    if decision["decision"] == "reassess" and any(
        i.endswith("-PRICE-01") for i in decision["condition_ids"]
    ):
        raise ValueError("This test has no qualified mechanical price trigger.")
    return (
        value,
        dict(
            action="recommend_reassessment"
            if decision["decision"] == "reassess"
            else "continue_monitoring",
            execute=False,
            coverage_gaps=gaps,
        ),
        packet,
        calls,
    )


# Record accepted changes and build the handoff for the next monitoring cycle.
def advance(state, value, handoff, cutoff):
    result = read_copy(state)
    prior = {e["event_key"]: e for e in state["prior_events"]}
    for event in value["events"]:
        if event["change"] != "duplicate" or event["event_key"] not in prior:
            prior[event["event_key"]] = event
    decision = read_copy(value["decision"])
    by_id = {e["id"]: e for e in state["prior_events"] + value["events"]}
    event_keys = sorted({by_id[i]["event_key"] for i in value["decision"]["evidence_ids"]})
    decision["evidence_ids"] = sorted(prior[key]["id"] for key in event_keys)
    group = live.compact(sorted(set(decision["condition_ids"])))
    previous = state["attention"].get(group, {})
    known_versions = {
        version
        for conditions, attention in state["attention"].items()
        if set(decision["condition_ids"]) <= set(json.loads(conditions))
        for version in attention.get("evidence_versions", [])
    }
    considered = state.get("considered_referrals", [])
    covered = {c for record in considered for c in record["condition_ids"]}
    if set(decision["condition_ids"]) <= covered:
        known_versions.update(
            version
            for record in considered
            if set(decision["condition_ids"]) & set(record["condition_ids"])
            for version in record["evidence_versions"]
        )
    versions = set(decision["evidence_ids"])
    alert = decision["decision"] == "reassess"
    new = alert and not versions <= known_versions
    if alert:
        alerted_versions = sorted(known_versions | versions)
        result["attention"][group] = dict(
            signature=digest(live.compact([group, alerted_versions])),
            evidence_versions=alerted_versions,
            event_keys=sorted(set(previous.get("event_keys", [])) | set(event_keys)),
            reason=decision["reason"],
        )
        if value.get("referral_qualification"):
            result["attention"][group].update(
                qualified=value["referral_qualification"]["qualified"],
                referral_case=value["referral_case"],
            )
    if value.get("referral_qualification"):
        result["referral_policy"] = value["referral_qualification"]["policy"]
    last_decision = {k: v for k, v in value["decision"].items() if k != "evidence_ids"}
    last_decision["evidence_event_keys"] = event_keys
    result.update(
        prior_events=list(prior.values()),
        last_decision=last_decision,
        last_observation_at=cutoff,
        last_completed_cycle_at=cutoff,
        coverage_gaps=handoff["coverage_gaps"],
        decision_state_note="Last decision keeps stable event keys; exact evidence IDs and prior versions remain in the cycle result.",
    )
    leads = {e["event_key"]: e for e in state.get("unverified_findings", [])}
    leads.update(
        {e["event_key"]: e for e in value.get("source_review", {}).get("unverified_findings", [])}
    )
    for event in value["events"]:
        leads.pop(event["event_key"], None)
    if leads or "unverified_findings" in state:
        result["unverified_findings"] = list(leads.values())
    for check in value["checks"]:
        if check["status"] == "checked":
            result["checkpoints"][check["id"]] = cutoff
    if state.get("considered_referrals"):
        result["baseline_continuation_completed_at"] = cutoff
    return result, dict(new=new, type=handoff["action"], execute=False)


# Make an independent JSON copy before updating the monitor state.
def read_copy(value):
    return json.loads(live.compact(value))


# Write an HTML summary of the monitoring result for inspection.
def render(folder, result):
    write(
        folder / "summary.html",
        '<!doctype html><meta charset="utf-8"><title>Fresh Sol continuation test</title>'
        "<style>body{max-width:1100px;margin:2em auto;font:17px/1.5 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>"
        "<h1>Fresh Sol continuation test</h1><p>Shadow decisions only; live collection and judgments need review.</p>"
        "<pre>" + escape(dump(result)) + "</pre>",
    )


# Freeze the inputs for a new monitor sequence or a successor sequence.
def prepare(folder, root=ROOT, successor=False):
    watch, seed, dependencies = inputs(root, successor)
    extra = (
        ("monitor_successor.sh",)
        if successor
        else tuple(FIXTURE + "/" + n for n in (*DATA, "provenance.json"))
    )
    cycles = ("successor-1",) if successor else CYCLES
    files = tuple(dict.fromkeys((*BASE_FILES, *dependencies, *extra)))
    manifest = dict(
        schema="monitor-delta-experiment-v1",
        model=MODEL,
        settings=live.SETTINGS,
        profile="successor" if successor else "delta",
        cycles=list(cycles),
        files={n: digest((root / n).read_bytes()) for n in files},
    )
    manifest["fingerprint"] = digest(live.compact(manifest))
    if (folder / "manifest.json").exists():
        if read(folder / "manifest.json") != manifest:
            raise ValueError(
                "This test has different frozen inputs; preserve it and use another version."
            )
    else:
        for name in files:
            target = folder / "frozen" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / name).read_bytes())
        write(folder / "manifest.json", manifest)
        write(folder / "initial-state.json", seed)
        write(folder / "state.json", seed)
        write(
            folder / "mechanical.json",
            scope.prices.replay(read(root / "fixtures/prices/replay.json")),
        )
        write(
            folder / "packages.json",
            {n: version(n) for n in ("openai", "pydantic", "pandas-market-calendars")},
        )
        write(
            folder / "preflight.json",
            dict(
                status="prepared_not_run",
                model=MODEL,
                cycles=list(cycles),
                model_generations_at_most=len(cycles),
                broker_snapshot_attempts_at_most=len(cycles),
                automatic_retries=0,
                max_output_tokens=live.SETTINGS["max_output_tokens"],
                reasoning=live.SETTINGS["reasoning"],
                max_tool_calls=None,
                billing_cap_usd=None,
                two_hour_overlap=True,
                execute=False,
            ),
        )
        write(folder / "result.json", dict(status="not_attempted", execute=False))
    verify(folder)
    if read(folder / "initial-state.json") != seed:
        raise ValueError("Initial actual-event state changed.")
    mechanical = read(folder / "mechanical.json")
    if mechanical["passed"] != mechanical["total"]:
        raise ValueError("Synthetic mechanical regression failed.")
    render(folder, read(folder / "result.json"))
    print(
        f"Prepared {len(cycles)} Sol fresh-search cycle(s); max/128,000; at most one read-only IBKR snapshot per cycle when session/access allow.",
        flush=True,
    )
    print(
        "No local research/spending cap; zero automatic retries. Each cycle stops after finite triage; shadow decisions only.",
        flush=True,
    )


# Check the saved sequence against its manifest and return the accepted watch.
def verify(folder):
    manifest = read(folder / "manifest.json")
    identity = {k: v for k, v in manifest.items() if k != "fingerprint"}
    if (
        manifest["model"] != MODEL
        or manifest["settings"] != live.SETTINGS
        or digest(live.compact(identity)) != manifest["fingerprint"]
    ):
        raise ValueError("Frozen delta experiment identity changed.")
    successor = manifest.get("profile") == "successor"
    if manifest.get("profile") not in ("delta", "successor") or manifest["cycles"] != list(
        ("successor-1",) if successor else CYCLES
    ):
        raise ValueError("Frozen continuation profile changed.")
    for name, expected in manifest["files"].items():
        if digest((folder / "frozen" / name).read_bytes()) != expected:
            raise ValueError("Frozen delta dependency changed: " + name)
    for name in BASE_FILES:
        if digest((ROOT / name).read_bytes()) != manifest["files"][name]:
            raise ValueError("Use the frozen delta runner and its matching dependencies.")
    watch, seed, _ = inputs(folder / "frozen", successor)
    if read(folder / "initial-state.json") != seed:
        raise ValueError("Initial-state binding changed.")
    return watch


# Run the planned monitoring cycles under a lock and save their results.
def run(folder, client=None, broker=True, times=None):
    with (folder / ".chain.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        watch = verify(folder)
        cycles = read(folder / "manifest.json")["cycles"]
        results = []
        for index, cycle in enumerate(cycles):
            target = folder / "cycles" / cycle
            state = live.recover_state(folder, watch)
            if not (target / "attempt.json").exists():
                expected = (
                    read(folder / "initial-state.json")
                    if index == 0
                    else results[index - 1]["next_state"]
                )
                if state != expected:
                    raise ValueError("Next cycle must consume the exact previously accepted state.")
                snapshot = target / "state-before.json"
                if snapshot.exists() and read(snapshot) != state:
                    raise ValueError("Saved pre-dispatch state differs; do not overwrite it.")
                if not snapshot.exists():
                    write(snapshot, state)
            result = live.run_cycle(
                folder,
                cycle,
                client=client,
                broker=broker,
                now=times[index] if times else None,
                verifier=verify,
                requester=request_for,
                validator=validate_observation,
                advancer=advance,
                renderer=render,
            )
            results.append(result)
            if (target / "request.json").exists():
                payload = json.loads(read(target / "request.json")["input"])
                write(target / "price_check.json", payload["price_check"])
                if payload["state_before_sha256"] != digest(
                    live.compact(read(target / "state-before.json"))
                ):
                    raise ValueError("Cycle did not consume its exact saved prior state.")
            if result["status"] != "completed":
                break
            write(target / "state-after.json", result["next_state"])
        summary = dict(
            status="completed"
            if len(results) == len(cycles) and all(x["status"] == "completed" for x in results)
            else "failed",
            execute=False,
            cycles=results,
            final_state_sha256=digest(live.compact(read(folder / "state.json"))),
            review="Pending source, novelty and finite-triage judgment review.",
        )
        write(folder / "result.json", summary)
        render(folder, summary)
        print(
            "Continuation test " + summary["status"] + "; HTML: " + str(folder / "summary.html"),
            flush=True,
        )
        return summary


# Dispatch the command to prepare, check, or run a monitoring sequence.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--successor",
        action="store_true",
        help="Prepare one cycle on the reviewed returned Big Seven baseline.",
    )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output.resolve(), successor=args.successor)
    elif run(args.output.resolve())["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
