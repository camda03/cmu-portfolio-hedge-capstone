"""One SOL discovery sweep shared by the unchanged seven analyst stages."""

from datetime import timedelta
from html import escape
import os
from pathlib import Path

import monitor_delta as delta
import monitor_live as live
from monitor_handoff import continuity_events, discovery_history
from monitor import digest, read, write

ROOT = Path(__file__).resolve().parent


class Discovery(live.Strict):
    checks: list[live.Check]
    events: list[delta.Event]


GUIDANCE = """
ADDITIONAL SHARED DISCOVERY: Keep every original analyst assignment and web-search
requirement. The SOL discovery packet is additional fallible evidence, not a fixed
source restriction or a replacement for your own searches. Investigate its material
findings, contrary evidence and gaps in your assigned role. A source URL or completed
scan does not establish truth or exhaustive coverage. Distinguish new public
disclosures from older evidence first discovered now and duplicates. For material
policy questions, investigate actual implementation and operations as well as legal
status; use relevant local-language specialist reporting and primary sources.
The Critic must examine discovery coverage of consequential unresolved questions
and perform differently worded follow-up searches where useful. The final Revision
must address every discovery event AND every coverage.unverified_findings entry,
and preserve unresolved material issues in the
handoff. In the existing single forecast JSON block, add discovery_dispositions:
one object per discovery event or unverified finding, with event_id, disposition (incorporated, rejected,
or unresolved), a substantive reason, and nonempty monitor_condition_ids (IDs in
the new structured monitoring handoff, or UNLISTED when none applies or only a
prose handoff is being produced). This extends the original JSON contract without
removing any required fields. Include
an empty list only when both lists are empty. Explain these dispositions and remaining
coverage gaps in a short "Shared discovery review" section of the final prose.
When a structured monitoring handoff is requested, Draft must include the proposed
monitoring_handoff in its forecast JSON using the supplied schema and new forecast ID.
It is a proposal for the closing discovery sweep, not an accepted baseline.
Critic and Revision receive closing evidence after Draft; use the explicitly supplied
closing cutoff for those stages while retaining earlier cards as dated analysis.
Use discovery_dispositions for the CURRENT packet only; discuss historical findings separately.
Do not invent additional incoming monitor referrals or change their required IDs.
Unverified findings are leads whose cited URLs could not all be matched to the
provider's search records. They are not validated evidence, even when some links
were traced. Independently research them before incorporating any claim; otherwise
reject them or retain them as unresolved. Do not infer a replacement URL from a
similar title, and do not treat a traced URL alone as proof of the claim.
Any unverified_evidence_ids in an incoming monitoring request also identify
historical leads requiring independent support, not confirmed trigger evidence.
"""


def checks_for(symbol, handoff=None, previous=None):
    """One discovery plan for baseline closure and subsequent monitoring."""
    if previous:
        ids = {c["id"] for c in handoff["conditions"] if not c["id"].endswith("-PRICE-01")}
        checks = [
            dict(
                old,
                condition_ids=[
                    c["id"]
                    for c in handoff["conditions"]
                    if c["id"] in ids
                    and ({c["id"]} | set(c["aliases"])) & set(old["condition_ids"])
                ],
            )
            for old in previous
        ]
        missing = ids - {i for c in checks for i in c["condition_ids"]}
        extra = next((c for c in checks if c["id"] == symbol + "-ADDITIONAL"), None)
        if missing and extra:
            extra["condition_ids"] = sorted(set(extra["condition_ids"]) | missing)
        elif missing:
            checks.append(
                dict(
                    id=symbol + "-ADDITIONAL",
                    topic="Newly added material conditions",
                    condition_ids=sorted(missing),
                    source_urls=[],
                    query=checks[0]["query"],
                )
            )
        return checks
    topics = [
        ("ISSUER", "Issuer disclosures and management"),
        ("INDUSTRY", "Peers, customers and suppliers"),
        ("POLICY", "Policy, law and operational implementation"),
        ("MACRO", "Macro, energy and finance"),
        ("WORLD", "Unexpected material developments"),
    ]
    ids = [
        c["id"] for c in (handoff or {}).get("conditions", []) if not c["id"].endswith("-PRICE-01")
    ]
    return [
        dict(
            id=symbol + "-" + key,
            topic=topic,
            condition_ids=ids if key != "WORLD" else [],
            source_urls=[],
            query="Check the supplied handoff premises and thresholds, original sources, material omissions and new disclosures. "
            "Use primary and relevant local-language sources, retain contrary evidence and distinguish duplicates.",
        )
        for key, topic in topics
    ]


def request_for(symbol, cutoff, cycle, context, watch=None, trigger=None, proposed=None):
    bound_monitor = trigger and trigger.get("schema") in ("monitor-return-v1", "monitor-return-v2")
    if bound_monitor:
        if (
            not watch
            or watch["symbol"] != symbol
            or watch["forecast"]["forecast_id"] != trigger["original_forecast"]["forecast_id"]
            or watch["handoff"] != trigger["accepted_handoff"]
            or trigger["monitor_state"]["watch_sha256"] != digest(live.compact(watch))
        ):
            raise ValueError("Discovery requires the exact active monitor watch and trigger state.")
        request, payload = delta.request_for(
            watch,
            trigger["monitor_state"],
            cutoff,
            cycle,
            {
                "status": "not_requested",
                "note": "Use only the validated broker context supplied below.",
            },
        )
        source = "active_monitor_watch"
    else:
        if watch:
            raise ValueError("An active discovery watch needs its bound monitoring trigger.")
        # First baselines have no accepted watch. Use the same five discovery
        # categories; subsequent reassessments use their exact live watch above.
        topics = [
            ("ISSUER", "Company disclosures, filings, management and customer developments"),
            ("INDUSTRY", "Peers, customers, suppliers and industry operating conditions"),
            ("POLICY", "Policy, regulation, legal decisions and actual implementation"),
            ("MACRO", "Macroeconomics, credit, energy and financing conditions"),
            ("WORLD", "Unexpected material developments and transmission channels"),
        ]
        checks = [
            dict(id=symbol + "-" + key, topic=topic, condition_ids=[], navigation_targets=[])
            for key, topic in topics
        ]
        payload = dict(
            checks=checks,
            requested_windows={c["id"]: dict(since=None, as_of=cutoff) for c in checks},
            as_of=cutoff,
            as_of_new_york=live.instant(cutoff)
            .astimezone(live.ZoneInfo("America/New_York"))
            .isoformat(),
            event_id_prefix=cycle + "-",
            prior_events=[],
            prior_coverage_gaps=[],
            baseline_note="No structured monitor watch supplied. Discover a baseline from the research context; "
            "no complete historical coverage or prior event ledger is claimed.",
        )
        request, source = dict(live.SETTINGS), "baseline_discovery_categories"
    if proposed is not None:
        from monitor_handoff import handoff_structure

        handoff = handoff_structure(proposed["handoff"], proposed["forecast_id"])
        if proposed["symbol"] != symbol:
            raise ValueError("Closing discovery subject differs from the proposed handoff.")
        checks = [
            dict(
                {k: v for k, v in c.items() if k != "source_urls"},
                navigation_targets=c["source_urls"],
            )
            for c in checks_for(symbol, handoff, watch["checks"] if watch else None)
        ]
        prior = {e["event_key"]: e for e in payload["prior_events"]}
        prior.update({e["event_key"]: e for e in proposed["prior_events"]})
        prior_forecast = payload.get("forecast", {})
        payload.update(
            handoff=handoff,
            checks=checks,
            proposed_handoff=handoff,
            requested_windows={
                c["id"]: dict(since=proposed["original_cutoff"], as_of=cutoff) for c in checks
            },
            prior_events=list(prior.values()),
            forecast=dict(
                proposed["forecast"], discovery_history=discovery_history(prior_forecast)
            ),
            baseline_note="Closing sweep against the actual proposed handoff, before Critic and Revision. "
            "Check consequential older omissions and disclosures since the original cutoff. "
            "Every finding is supplied to both analysts; this is not an accepted forecast.",
        )
        source = "proposed_monitor_handoff"
    payload.update(subject_symbol=symbol, research_context=context, discovery_scope_source=source)
    instructions = (
        (ROOT / "monitor_delta_prompt.md").read_text()
        + "\n\n"
        + (ROOT / "analyst_discovery_prompt.md").read_text()
    )
    request.update(
        instructions=instructions,
        input=live.compact(payload),
        text={
            "format": dict(
                type="json_schema",
                name="analyst_discovery",
                strict=True,
                schema=Discovery.model_json_schema(),
            )
        },
    )
    if os.environ.get("CAPSTONE_DAILY_RESEARCH") == "1":
        # Stream foreground research and save its provider ID as soon as it is assigned.
        request.update(background=False, store=True, stream=True)
    return request, payload


def validate(raw, text, watch, state, payload, model):
    if (
        raw.get("status") != "completed"
        or not (raw.get("model") == model or (raw.get("model") or "").startswith(model + "-"))
        or raw.get("service_tier") not in (None, "default")
        or any(
            x.get("type", "").endswith("_call") and x["type"] != "web_search_call"
            for x in raw.get("output", [])
        )
    ):
        raise ValueError("Discovery requires a completed SOL response using only web search.")
    value = Discovery.model_validate_json(text).model_dump()
    traces, calls = delta.source_trace(raw)
    normalizations, rejections, unverified = delta.match_sources(value, traces)
    expected = {c["id"] for c in payload["checks"]}
    if len(value["checks"]) != len(expected) or {c["id"] for c in value["checks"]} != expected:
        raise ValueError("Discovery must report every monitoring check group exactly once.")
    for check in value["checks"]:
        if not check["note"].strip():
            raise ValueError("Discovery coverage needs an explanation.")
    old = continuity_events(payload.get("forecast", {}), payload["prior_events"])
    ids, keys = {e["id"] for e in old.values()}, set()
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
            raise ValueError("Discovery finding has invalid identity or continuity: " + event["id"])
        if event["published_at"] is not None and live.instant(event["published_at"]) > live.instant(
            payload["as_of"]
        ):
            raise ValueError("Discovery evidence was published after the common research cutoff.")
        ids.add(event["id"])
        keys.add(event["event_key"])
    rejected = {e["id"] for e in unverified}
    value["events"] = [e for e in value["events"] if e["id"] not in rejected]
    coverage = dict(
        source_trace=traces,
        gaps=[c for c in value["checks"] if c["status"] != "checked"],
        inherited_gaps=payload.get("prior_coverage_gaps", []),
        source_rejections=rejections,
        source_normalizations=normalizations,
        unverified_findings=unverified,
        exhaustive=False,
    )
    return value, {"execute": False}, {"coverage": coverage}, calls


def collect(
    folder,
    symbol,
    cutoff,
    cycle,
    context,
    watch=None,
    trigger=None,
    client=None,
    proposed=None,
    resume=False,
):
    request, payload = request_for(symbol, cutoff, cycle, context, watch, trigger, proposed)
    folder.mkdir(parents=True, exist_ok=True)
    # These validity fields describe one fixed-cutoff collection session, not a
    # financial forecast. The original watch and its dates remain in the request.
    session = dict(
        schema="analyst-discovery-session-v1",
        symbol=symbol,
        issued_at=cutoff,
        valid_until=(live.instant(cutoff) + timedelta(seconds=1)).isoformat(),
        request=request,
    )
    if resume:
        saved = read(folder / "watch.json")
        target = folder / "cycles" / cycle
        result, raw = read(target / "result.json"), read(target / "response.json")
        # A completed request keeps the instructions actually sent. Only that
        # instruction text may differ; inputs, model, schema and limits must match.
        expected = dict(
            session, request=dict(request, instructions=saved["request"]["instructions"])
        )
        if (
            saved != expected
            or read(target / "request.json") != saved["request"]
            or result["status"] != "completed"
            or result["as_of"] != cutoff
            or not raw.get("id")
            or result["response_id"] != raw.get("id")
            or read(target / "attempt.json").get("response_id") not in (None, raw.get("id"))
        ):
            raise ValueError("Saved discovery inputs changed or response is not completed.")
        text = "".join(
            c.get("text", "")
            for item in raw.get("output", [])
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        )
        value, _, evidence, _ = validate(raw, text, saved, {}, payload, delta.MODEL)
        if value != result["observation"] or evidence["coverage"] != result["coverage"]:
            raise ValueError("Saved discovery no longer matches validated response evidence.")
        session, request = saved, saved["request"]
    elif (folder / "watch.json").exists() and read(folder / "watch.json") != session:
        raise ValueError("Saved discovery inputs changed; reconcile the existing attempt.")
    else:
        write(folder / "watch.json", session)
    write(folder / "manifest.json", {"model": delta.MODEL})

    def render(target, result):
        write(
            target / "summary.html",
            "<pre>"
            + escape(
                live.compact(
                    {
                        k: result.get(k)
                        for k in ("status", "as_of", "coverage", "cost", "note", "validation_error")
                    }
                )
            )
            + "</pre>",
        )

    result = live.run_cycle(
        folder,
        cycle,
        client=client,
        broker=False,
        now=cutoff,
        verifier=lambda _: session,
        requester=lambda *_: (request, payload),
        validator=validate,
        advancer=lambda state, *_: (state, {"new": False, "execute": False}),
        renderer=render,
    )
    if result["status"] != "completed":
        raise ValueError(
            "Shared discovery did not complete; review saved evidence before another request."
        )
    packet = dict(
        schema="analyst-discovery-v1",
        symbol=symbol,
        as_of=cutoff,
        scope_source=payload["discovery_scope_source"],
        request_sha256=digest(live.compact(request)),
        watch_sha256=digest(live.compact(watch)) if watch else None,
        monitor_state_sha256=digest(live.compact(trigger["monitor_state"])) if watch else None,
        response_id=result["response_id"],
        model=delta.MODEL,
        received_at=result["received_at"],
        **result["observation"],
        coverage=result["coverage"],
        usage=result["usage"],
        cost=result["cost"],
    )
    if proposed is not None:
        packet["proposed_handoff"] = payload["proposed_handoff"]
    write(folder / "packet.json", packet)
    return packet


def attach(forecast, packet, previous=None):
    """Require review of every finding before publishing an analyst baseline."""
    items = forecast.get("discovery_dispositions")
    ids = {e["id"] for e in packet["events"] + packet["coverage"].get("unverified_findings", [])}
    if previous is not None:
        forecast["discovery_history"] = discovery_history(previous)
        forecast.pop("historical_discovery_dispositions", None)
    known = {item["event"]["id"] for item in forecast.get("discovery_history", [])}
    if isinstance(items, list) and all(isinstance(item, dict) for item in items):
        extras = [item for item in items if item.get("event_id") not in ids]
        history_items = forecast.get("historical_discovery_dispositions", []) + extras
        if any(item.get("event_id") not in known for item in history_items) or len(
            {item.get("event_id") for item in history_items}
        ) != len(history_items):
            raise ValueError(
                "Historical discovery dispositions must reference distinct known findings."
            )
        items = [item for item in items if item.get("event_id") in ids]
        forecast["discovery_dispositions"] = items
        if history_items:
            forecast["historical_discovery_dispositions"] = history_items
    allowed = {c["id"] for c in forecast.get("monitoring_handoff", {}).get("conditions", [])} | {
        "UNLISTED"
    }
    if (
        packet["schema"] != "analyst-discovery-v1"
        or packet["model"] != delta.MODEL
        or packet["symbol"] != forecast["symbol"]
        or packet["as_of"] != forecast["information_cutoff"]
        or not isinstance(items, list)
        or len(items) != len(ids)
        or any(not isinstance(x, dict) for x in items)
        or {x.get("event_id") for x in items} != ids
    ):
        raise ValueError(
            "Every shared discovery finding needs exactly one final disposition at the same cutoff."
        )
    for item in items + forecast.get("historical_discovery_dispositions", []):
        conditions = item.get("monitor_condition_ids")
        if (
            item.get("disposition") not in ("incorporated", "rejected", "unresolved")
            or not isinstance(item.get("reason"), str)
            or not item["reason"].strip()
            or not isinstance(conditions, list)
            or not conditions
            or not all(isinstance(c, str) for c in conditions)
            or len(set(conditions)) != len(conditions)
            or not set(conditions) <= allowed
        ):
            raise ValueError(
                "Discovery disposition needs a substantive reason and valid monitoring conditions."
            )
    forecast["shared_discovery"] = packet


def carry_forward(watch, state, forecast):
    """Remember considered evidence versions without suppressing future changes."""
    packet = forecast.get("shared_discovery")
    if packet is None:
        return  # Historical baselines remain historical, without invented scans.
    attach(forecast, packet)
    events = {e["event_key"]: e for e in state["prior_events"]}
    findings = {
        e["id"]: e for e in packet["events"] + packet["coverage"].get("unverified_findings", [])
    }
    traced = {e["id"] for e in packet["events"]}
    ordered = {
        e["id"]: i
        for i, e in enumerate(packet["events"] + packet["coverage"].get("unverified_findings", []))
    }
    for item in sorted(forecast["discovery_dispositions"], key=lambda x: ordered[x["event_id"]]):
        event = findings[item["event_id"]]
        if event["id"] in traced and event["change"] != "duplicate":
            events[event["event_key"]] = event
        state["considered_referrals"].append(
            dict(
                referral_id="discovery-" + event["id"],
                disposition=item["disposition"],
                reason=item["reason"],
                condition_ids=item["monitor_condition_ids"],
                source_condition_ids=item["monitor_condition_ids"],
                event_keys=[event["event_key"]],
                evidence_versions=[e for e in (event["previous_event_id"], event["id"]) if e],
                considered_by_forecast_id=forecast["forecast_id"],
                risk_remains_active=True,
            )
        )
    state["prior_events"] = list(events.values())
    watch["baseline_reassessment"]["shared_discovery"] = dict(
        as_of=packet["as_of"],
        checks=packet["checks"],
        coverage=packet["coverage"],
        dispositions=forecast["discovery_dispositions"],
    )


def closing_packet(initial, closing):
    """Keep both sweeps and every finding; analysts must disposition all of them."""
    if (
        initial["symbol"] != closing["symbol"]
        or closing["scope_source"] != "proposed_monitor_handoff"
        or live.instant(closing["as_of"]) < live.instant(initial["as_of"])
    ):
        raise ValueError("Closing discovery chronology or identity differs.")
    packet = dict(
        closing,
        scope_source="baseline_and_closing_discovery",
        components=[initial, closing],
        events=initial["events"] + closing["events"],
        watch_sha256=initial.get("watch_sha256"),
        monitor_state_sha256=initial.get("monitor_state_sha256"),
    )
    packet["coverage"] = dict(
        closing["coverage"],
        unverified_findings=initial["coverage"].get("unverified_findings", [])
        + closing["coverage"].get("unverified_findings", []),
    )
    packet["coverage"]["source_trace"] = {
        **initial["coverage"].get("source_trace", {}),
        **closing["coverage"].get("source_trace", {}),
    }
    packet["coverage"]["inherited_gaps"] = initial["coverage"].get("gaps", []) + initial[
        "coverage"
    ].get("inherited_gaps", [])
    ids = [e["id"] for e in packet["events"] + packet["coverage"]["unverified_findings"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Closing discovery reused a finding identity.")
    return packet


def saved_packet(output):
    """Check both saved sweeps before accepting a report that claims closing coverage."""
    from monitor import read

    initial = read(output / "discovery/packet.json")
    if not (output / "closing-discovery/combined.json").exists():
        return initial
    closing = read(output / "closing-discovery/packet.json")
    combined = read(output / "closing-discovery/combined.json")
    if combined != closing_packet(initial, closing):
        raise ValueError("Combined discovery differs from its two saved source packets.")
    return combined
