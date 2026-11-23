"""Validate a saved monitoring request before it becomes analyst context."""

import json
import re

from monitor import checked_decision, dump, digest as content_digest
from monitor_inputs import forecast_fields
from price_watch import digest, instant
from securities import SUBJECTS


# Check the referral's subject, cutoff, and origin before it enters a reassessment.
def check_trigger(value, symbol, cutoff, context_only=False):
    """Return lineage fields; synthetic requests are restricted to context preparation."""
    try:
        if value.get("schema") in ("monitor-return-v1", "monitor-return-v2"):
            return check_return(value, symbol, cutoff)
        mode, original = value["evidence_mode"], value["original_forecast"]
        if (
            value["schema"] != "monitor-handoff-v1"
            or mode not in ("synthetic", "live_shadow")
            or not isinstance(value["request_id"], str)
            or not value["request_id"].strip()
            or value["symbol"] != symbol
            or original["symbol"] != symbol
            or not isinstance(original["forecast_id"], str)
            or not original["forecast_id"]
            or value["original_forecast_sha256"] != digest(original)
            or not re.fullmatch("[0-9a-f]{64}", value["watch_sha256"])
        ):
            raise ValueError("Monitoring request identity or frozen forecast hash does not match.")
        as_of, issued = instant(value["as_of"]), instant(original["issued_at"])
        if not issued <= as_of <= cutoff:
            raise ValueError(
                "Monitoring request exceeds the analyst cutoff or precedes its forecast."
            )
        if mode == "synthetic" and not context_only:
            raise ValueError(
                "Synthetic monitoring requests require --context-only; no model calls allowed."
            )
        report = value.get("original_report")
        if "original_report" in value or "original_report_sha256" in value:
            if not isinstance(report, str) or value.get("original_report_sha256") != digest(report):
                raise ValueError("The frozen original report hash does not match.")
        if mode == "live_shadow":
            if (
                original.get("synthetic") is True
                or original.get("forecast_mode") != "operational"
                or not isinstance(report, str)
                or not report.strip()
            ):
                raise ValueError(
                    "A live shadow request requires the original operational forecast and report."
                )
            fields = forecast_fields(original)
        if not isinstance(value["updates"], list) or not value["updates"]:
            raise ValueError("Monitoring request has no supporting observations.")
        supported, seen = set(), {}
        for update in value["updates"]:
            packet, watchlist = update["packet"], update["watchlist"]
            if (
                packet["symbol"] != symbol
                or watchlist["symbol"] != symbol
                or watchlist["id"] != original["forecast_id"]
                or packet["evidence_mode"] != mode
                or (mode == "live_shadow" and watchlist.get("synthetic") is not False)
            ):
                raise ValueError("Monitoring evidence belongs to another forecast, symbol or mode.")
            if mode == "live_shadow" and (
                any(
                    instant(watchlist[k]) != instant(fields[k])
                    for k in ("issued_at", "valid_until")
                )
                or any(
                    not c.get("source_quote") or c["source_quote"] not in report
                    for c in watchlist["conditions"]
                )
            ):
                raise ValueError(
                    "Live premises must match the original forecast dates and quote its frozen report."
                )
            since, through = instant(packet["since"]), instant(packet["as_of"])
            if not issued <= since <= through <= as_of:
                raise ValueError("Monitoring interval is outside the frozen forecast and cutoff.")
            if (
                packet["coverage"].get("checked_through")
                and instant(packet["coverage"]["checked_through"]) > through
            ):
                raise ValueError("Coverage extends beyond the evidence cutoff.")
            decision = checked_decision(json.dumps(update["decision"]), watchlist, packet)
            for event in packet["prior_events"] + packet["events"]:
                available = instant(event["available_at"])
                if available > (since if event in packet["prior_events"] else through):
                    raise ValueError("Evidence was unavailable at the claimed cutoff.")
                if (
                    event.get("published_at")
                    and instant(event["published_at"]) > available
                    or event.get("collected_at")
                    and not available <= instant(event["collected_at"]) <= through
                ):
                    raise ValueError("Evidence publication or collection chronology is invalid.")
                if mode == "live_shadow" and (
                    event.get("synthetic") is not False
                    or not event.get("url", "").startswith(("https://", "http://"))
                    or not event.get("collected_at")
                ):
                    raise ValueError(
                        "Live shadow evidence requires nonsynthetic provenance, source URL and collection time."
                    )
                if event["id"] in seen and seen[event["id"]] != digest(event):
                    raise ValueError("An evidence identity changed within the monitoring request.")
                seen[event["id"]] = digest(event)
            if decision["decision"] == "reassess":
                supported.update("news:" + event_id for event_id in decision["evidence_ids"])
            observation, result = update["observation"], update["price_result"]
            if result.get("forecast_id") != original["forecast_id"]:
                raise ValueError("Price evaluation belongs to another forecast.")
            if mode == "live_shadow" and observation.get("data_mode") == "REPLAY":
                raise ValueError("Replay prices cannot become live shadow evidence.")
            price_signals = [s for s in result["signals"] if s.startswith("price:")]
            if any(
                s
                not in (
                    "price:cumulative:up",
                    "price:cumulative:down",
                    "price:recent:up",
                    "price:recent:down",
                )
                for s in price_signals
            ):
                raise ValueError("Unknown price warning type.")
            if price_signals:
                if (
                    result["price_status"] != "valid"
                    or observation.get("symbol") != symbol
                    or observation.get("data_mode") not in ("REALTIME", "REPLAY")
                    or observation.get("is_close")
                ):
                    raise ValueError(
                        "A price warning requires a validated, timestamped observation."
                    )
                times = [
                    instant(observation[k]) for k in ("observed_at", "received_at", "checked_at")
                ]
                if not issued <= times[0] <= times[1] <= times[2] <= as_of:
                    raise ValueError(
                        "Price warning chronology exceeds the forecast or monitoring cutoff."
                    )
            # Rejected prices without a price signal remain coverage diagnostics;
            # malformed quote fields must not suppress independently valid news.
            supported.update(price_signals)
        if (
            not isinstance(value["signals"], list)
            or not value["signals"]
            or any(not isinstance(s, str) for s in value["signals"])
            or not set(value["signals"]) <= supported
        ):
            raise ValueError("Monitoring request contains an unsupported signal.")
        return dict(
            predecessor_forecast_id=original["forecast_id"],
            monitor_request_id=value["request_id"],
            monitor_trigger_sha256=digest(value),
            monitor_evidence_mode=mode,
        )
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Monitoring request is incomplete or malformed.") from error


# Validate the text and list fields required in a set of handoff records.
def records(values, strings, lists=()):
    """Check the small text/list records shared with the existing handoff schema."""
    if not isinstance(values, list):
        raise ValueError("Handoff records must be a list.")
    for value in values:
        if (
            not isinstance(value, dict)
            or set(value) != set(strings) | set(lists)
            or any(not isinstance(value[k], str) or not value[k].strip() for k in strings)
            or any(
                not isinstance(value[k], list)
                or any(not isinstance(s, str) or not s.strip() for s in value[k])
                or len(set(value[k])) != len(value[k])
                for k in lists
            )
        ):
            raise ValueError("A handoff record has missing, extra or empty fields.")
    return values


# Check that the monitor handoff identifies a forecast and valid destinations for its findings.
def handoff_structure(value, forecast_id):
    """Validate structure and destinations; warning meaning still needs review."""
    keys = {
        "forecast_id",
        "baseline_summary",
        "conditions",
        "research_questions",
        "warning_continuity",
        "coverage_gaps",
        "price_policy",
    }
    if not isinstance(value, dict) or set(value) != keys or value["forecast_id"] != forecast_id:
        raise ValueError("Handoff fields or forecast identity do not match.")
    records(
        [
            {
                k: value[k]
                for k in ("forecast_id", "baseline_summary", "price_policy", "coverage_gaps")
            }
        ],
        ("forecast_id", "baseline_summary", "price_policy"),
        ("coverage_gaps",),
    )
    conditions = records(
        value["conditions"],
        ("id", "baseline", "reassess_when", "log_watch_when", "timing"),
        ("aliases", "source_refs"),
    )
    questions = records(
        value["research_questions"], ("id", "owner", "question", "status"), ("source_refs",)
    )
    ids = [item["id"] for item in conditions + questions]
    if (
        not conditions
        or len(set(ids)) != len(ids)
        or any(not item["source_refs"] for item in conditions + questions)
    ):
        raise ValueError("Require distinct handoff IDs and supporting sources.")
    continuity = records(
        value["warning_continuity"], ("id", "disposition", "reason"), ("destination_ids",)
    )
    if (
        not continuity
        or len({item["id"] for item in continuity}) != len(continuity)
        or any(
            item["disposition"] not in ("retained", "revised", "resolved", "superseded")
            or not item["destination_ids"]
            or not set(item["destination_ids"]) <= set(ids)
            for item in continuity
        )
    ):
        raise ValueError(
            "Warning continuity requires distinct IDs and valid explained destinations."
        )
    return value


# Extract the identities and source attribution of discoveries already considered.
def discovery_history(forecast):
    """Retain exact discovery identities and attribution without promoting leads."""
    history = {item["event"]["id"]: item for item in forecast.get("discovery_history", [])}
    packet = forecast.get("shared_discovery", {})
    for status, events in (
        ("provider_traced", packet.get("events", [])),
        ("unverified_lead", packet.get("coverage", {}).get("unverified_findings", [])),
    ):
        for event in events:
            item = dict(event=event, source_attribution=status)
            if event["id"] in history and history[event["id"]] != item:
                raise ValueError("Discovery history changed an existing evidence identity.")
            history[event["id"]] = item
    return list(history.values())


# Reconcile saved events with the discovery history carried by the forecast.
def continuity_events(forecast, prior_events):
    """Identity lookup only; decision validation still excludes unverified leads."""
    events = {item["event"]["event_key"]: item["event"] for item in discovery_history(forecast)}
    events.update({event["event_key"]: event for event in prior_events})
    return events


# List prior warnings and referrals that still require attention.
def pending_attention(state):
    """Historical attention remains visible; incorporated evidence is not a new job."""
    pending = {}
    for key, attention in state["attention"].items():
        if (
            state.get("referral_policy") == "baseline-comparison-v1"
            and attention.get("qualified") is not True
        ):
            continue
        ids = set(json.loads(key))
        records = state.get("considered_referrals", [])
        covered = {
            i for r in records for i in r["condition_ids"] + r.get("source_condition_ids", [])
        }
        considered = {
            v
            for r in records
            if ids <= covered and ids & set(r["condition_ids"] + r.get("source_condition_ids", []))
            for v in r["evidence_versions"]
        }
        if not set(attention["evidence_versions"]) <= considered:
            pending[tuple(sorted(ids))] = attention
    return pending


# Check that the returned referral matches the pending evidence and its current disposition.
def check_return(value, symbol, cutoff):
    """Bind the actual pending referrals and their current mitigation to the native run."""
    original, handoff, state = (
        value[k] for k in ("original_forecast", "accepted_handoff", "monitor_state")
    )
    report, provenance = value["original_report"], value["source_provenance"]
    identity = original["forecast_id"]
    fields = forecast_fields(original, price_basis="unverified")
    if (
        value["evidence_mode"] != "live_shadow"
        or value["symbol"] != symbol
        or original["symbol"] != symbol
        or original.get("forecast_mode") != "operational"
        or original.get("synthetic") is True
        or not isinstance(value["request_id"], str)
        or not value["request_id"].strip()
        or not isinstance(report, str)
        or not report.startswith("# " + SUBJECTS[symbol] + ":")
        or any(
            value[k + "_sha256"] != digest(value[k])
            for k in ("original_forecast", "original_report", "accepted_handoff", "monitor_state")
        )
        or state["forecast_id"] != identity
        or handoff["forecast_id"] != identity
        or state.get("pending_cycle")
        or state["handoff_sha256"] != content_digest(dump(handoff))
        or provenance["accepted_handoff_file_sha256"] != state["handoff_sha256"]
        or provenance["monitor_state_sha256"] != value["monitor_state_sha256"]
    ):
        raise ValueError(
            "Monitor return identity, frozen content or accepted-state binding is invalid."
        )
    handoff_structure(handoff, identity)
    as_of = instant(value["as_of"])
    if (
        not instant(original["issued_at"]) <= as_of <= cutoff
        or as_of > instant(fields["valid_until"])
        or instant(state["last_observation_at"]) != as_of
        or instant(state["last_completed_cycle_at"]) != as_of
        or any(instant(t) > as_of for t in state["checkpoints"].values())
        or any(
            not instant(source["as_of"]) <= as_of
            or not instant(source["as_of"]) <= instant(source["received_at"]) <= cutoff
            for source in provenance["sources"]
        )
    ):
        raise ValueError(
            "Monitor return chronology is outside the accepted forecast or analyst cutoff."
        )
    if (
        value["price_diagnostics"].get("qualified_price_trigger") is not False
        or value["price_diagnostics"].get("signals", [])
        or value["price_diagnostics"].get("execute") is not False
        or value["price_diagnostics"].get("watch_initialized") is not False
        or not isinstance(value["coverage_gaps"], list)
        or value["coverage_gaps"] != state["coverage_gaps"]
    ):
        raise ValueError(
            "This news return must preserve unqualified price diagnostics and coverage gaps."
        )
    current = continuity_events(original, state["prior_events"])
    conditions = {item["id"] for item in handoff["conditions"]}
    dynamic = value["schema"] == "monitor-return-v2"
    pending = (
        pending_attention(state)
        if dynamic
        else {
            tuple(sorted(json.loads(key))): attention
            for key, attention in state["attention"].items()
        }
    )
    if dynamic:
        conditions.add("UNLISTED")
    referrals, seen = value["referrals"], set()
    if (
        not isinstance(referrals, list)
        or not referrals
        or len({e["event_key"] for e in state["prior_events"]}) != len(state["prior_events"])
    ):
        raise ValueError("Monitor return needs distinct current events and pending referrals.")
    for referral in referrals:
        ids = tuple(sorted(referral["condition_ids"]))
        decision, old, latest = (
            referral["original_decision"],
            referral["original_events"],
            referral["current_events"],
        )
        attention = pending.get(ids)
        expected_keys = set(attention["event_keys"]) if attention else set()
        if dynamic:
            expected_keys.update(e["event_key"] for e in old)
        if (
            not isinstance(referral["id"], str)
            or not referral["id"].strip()
            or referral["id"] in seen
            or (not dynamic and referral["id"] not in ids)
            or not ids
            or len(set(ids)) != len(ids)
            or not set(ids) <= conditions
            or not attention
            or decision["decision"] != "reassess"
            or tuple(sorted(decision["condition_ids"])) != ids
            or decision["reason"] != attention["reason"]
            or set(decision["evidence_ids"]) != set(attention["evidence_versions"])
            or len(decision["evidence_ids"]) != len(attention["evidence_versions"])
            or set(referral["event_keys"]) != expected_keys
            or len(referral["event_keys"]) != len(expected_keys)
            or {e["id"] for e in old} != set(attention["evidence_versions"])
            or len(old) != len(attention["evidence_versions"])
            or len(latest) != len(expected_keys)
            or {e["event_key"] for e in old} != set(referral["event_keys"])
            or {e["event_key"] for e in latest} != set(referral["event_keys"])
            or any(e != current.get(e["event_key"]) for e in latest)
        ):
            raise ValueError(
                "Each return must preserve its actual alert and latest corresponding evidence."
            )
        seen.add(referral["id"])
        previous = {event["event_key"]: event for event in old}
        for event in latest:
            earlier = previous[event["event_key"]]
            if dynamic:
                # A durable hourly run can contain several revisions of one episode.
                history = value["evidence_history"]
                versions = {e["id"]: e for e in history}
                if len(versions) != len(history) or any(
                    versions.get(e["id"]) != e for e in old + latest
                ):
                    raise ValueError("Evidence history does not preserve the referred versions.")
                visited = set()
                while True:
                    if event["id"] in visited:
                        raise ValueError("Cyclic evidence version history.")
                    visited.add(event["id"])
                    if event.get("previous_event_id") not in versions:
                        break
                    parent = versions[event["previous_event_id"]]
                    if parent["event_key"] != event["event_key"]:
                        raise ValueError("Evidence version changed its episode identity.")
                    event = parent
                if not {e["id"] for e in old if e["event_key"] == event["event_key"]} <= visited:
                    raise ValueError("Current evidence is disconnected from a referred version.")
            elif event != earlier and event.get("previous_event_id") != earlier["id"]:
                raise ValueError("Updated evidence must link to the referred prior version.")
    used = {e["id"] for r in referrals for e in r["original_events"] + r["current_events"]}
    used.update(e["id"] for e in value.get("evidence_history", []))
    unverified = {
        item["event"]["id"]
        for item in discovery_history(original)
        if item["source_attribution"] == "unverified_lead"
    } & used
    if set(value.get("unverified_evidence_ids", [])) != unverified:
        raise ValueError("Returned discovery leads must remain explicitly unverified.")
    for event in (
        state["prior_events"]
        + [e for r in referrals for e in r["original_events"]]
        + value.get("evidence_history", [])
    ):
        if (
            not event["text"].strip()
            or not event["publication_label"].strip()
            or not event["source_urls"]
            or any(not url.startswith(("https://", "http://")) for url in event["source_urls"])
            or event.get("synthetic") is True
            or event.get("evidence_mode") == "synthetic"
            or event.get("published_at")
            and instant(event["published_at"]) > as_of
        ):
            raise ValueError(
                "Returned evidence must retain real source attribution and cutoff limits."
            )
    if {tuple(sorted(r["condition_ids"])) for r in referrals} != set(pending) or len(
        referrals
    ) != len(pending):
        raise ValueError("Every pending attention group needs exactly one referral.")
    return dict(
        predecessor_forecast_id=identity,
        monitor_request_id=value["request_id"],
        monitor_trigger_sha256=digest(value),
        monitor_evidence_mode="live_shadow",
    )


# Verify that the revised forecast accounts for returned referrals and outstanding warnings.
def check_return_output(forecast, trigger):
    """Require explicit referral dispositions and warning continuity before publication."""
    try:
        handoff = handoff_structure(forecast["monitoring_handoff"], forecast["forecast_id"])
        required = {item["id"] for item in trigger["accepted_handoff"]["warning_continuity"]}
        if not required <= {item["id"] for item in handoff["warning_continuity"]}:
            raise ValueError("The new handoff silently drops an inherited warning.")
        items = records(
            forecast["reassessment_dispositions"],
            ("referral_id", "disposition", "reason", "forecast_effect", "remaining_watch"),
            ("evidence_urls", "monitor_condition_ids", "incorporated_event_keys"),
        )
        referrals = {item["id"]: item for item in trigger["referrals"]}
        if len(items) != len(referrals) or {item["referral_id"] for item in items} != set(
            referrals
        ):
            raise ValueError("Every actual referral needs exactly one explicit disposition.")
        condition_ids = {item["id"] for item in handoff["conditions"]}
        for item in items:
            if (
                item["disposition"] not in ("accepted", "rejected", "modified", "unresolved")
                or (not item["evidence_urls"] and item["disposition"] != "unresolved")
                or any(not url.startswith(("https://", "http://")) for url in item["evidence_urls"])
                or not item["monitor_condition_ids"]
                or not set(item["monitor_condition_ids"]) <= condition_ids
                or set(item["incorporated_event_keys"])
                != set(referrals[item["referral_id"]]["event_keys"])
            ):
                raise ValueError(
                    "A referral disposition lacks valid evidence, current conditions or considered events."
                )
        return handoff, items
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(
            "The return handoff or referral dispositions are missing or malformed."
        ) from error
