"""Opening and closing news searches for one dated research report."""

from itertools import count
from time import monotonic, sleep
from zoneinfo import ZoneInfo
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .common import ROOT, SOL_MODEL, compact, digest, instant, rate_delay, stamp, write
from .evidence import continuity_events, discovery_history, handoff_structure, match_sources, source_trace

SETTINGS = dict(
    model=SOL_MODEL,
    reasoning={"effort": "max"},
    max_output_tokens=128000,
    service_tier="default",
    background=False,
    store=True,
    stream=True,
    tools=[
        {
            "type": "web_search",
            "external_web_access": True,
            "search_context_size": "high",
            "return_token_budget": "unlimited",
        }
    ],
    tool_choice="required",
    include=["web_search_call.action.sources"],
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Check(Strict):
    id: str
    status: Literal["checked", "partial", "failed", "not_checked"]
    source_urls: list[str]
    note: str


class Event(Strict):
    change: Literal["new", "update", "duplicate"]
    id: str
    event_key: str
    source_urls: list[str]
    published_at: str | None
    publication_label: str
    text: str
    previous_event_id: str | None


class Discovery(Strict):
    checks: list[Check]
    events: list[Event]


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


def checks_for(symbol, handoff):
    topics = [
        ("ISSUER", "Issuer disclosures and management"),
        ("INDUSTRY", "Peers, customers and suppliers"),
        ("POLICY", "Policy, law and operational implementation"),
        ("MACRO", "Macro, energy and finance"),
        ("WORLD", "Unexpected material developments"),
    ]
    ids = [c["id"] for c in handoff.get("conditions", []) if not c["id"].endswith("-PRICE-01")]
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


def request_for(symbol, cutoff, cycle, context, proposed=None):
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
        as_of_new_york=instant(cutoff).astimezone(ZoneInfo("America/New_York")).isoformat(),
        event_id_prefix=cycle + "-",
        prior_events=[],
        prior_coverage_gaps=[],
        baseline_note="No structured monitor watch supplied. Discover a baseline from the research context; "
        "no complete historical coverage or prior event ledger is claimed.",
    )
    request, source = dict(SETTINGS), "baseline_discovery_categories"
    if proposed is not None:
        handoff = handoff_structure(proposed["handoff"], proposed["forecast_id"])
        if proposed["symbol"] != symbol:
            raise ValueError("Closing discovery subject differs from the proposed handoff.")
        checks = [
            dict({k: v for k, v in c.items() if k != "source_urls"}, navigation_targets=c["source_urls"])
            for c in checks_for(symbol, handoff)
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
            forecast=dict(proposed["forecast"], discovery_history=discovery_history(prior_forecast)),
            baseline_note="Closing sweep against the actual proposed handoff, before Critic and Revision. "
            "Check consequential older omissions and disclosures since the original cutoff. "
            "Every finding is supplied to both analysts; this is not an accepted forecast.",
        )
        source = "proposed_monitor_handoff"
    payload.update(subject_symbol=symbol, research_context=context, discovery_scope_source=source)
    instructions = (
        (ROOT / "discovery_base_prompt.md").read_text() + "\n\n" + (ROOT / "discovery_prompt.md").read_text()
    )
    request.update(
        instructions=instructions,
        input=compact(payload),
        text={
            "format": dict(
                type="json_schema",
                name="analyst_discovery",
                strict=True,
                schema=Discovery.model_json_schema(),
            )
        },
    )
    return request, payload


def validate(raw, text, payload, model):
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
    traces, _ = source_trace(raw)
    normalizations, rejections, unverified = match_sources(value, traces)
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
        if event["published_at"] is not None and instant(event["published_at"]) > instant(payload["as_of"]):
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
    return value, coverage


def streamed_discovery(client, request, target, result):
    deadline = monotonic() + 7200
    for number in count():
        try:
            stream = client.responses.create(**request)
            break
        except Exception as error:
            delay = rate_delay(error, number)
            if delay is None or delay >= deadline - monotonic():
                raise
            result.update(phase="temporary_rate_limit", refused_429s=number + 1)
            write(target / "attempt.json", result)
            print(f"Discovery: temporary rate limit; retrying after {delay:.0f}s", flush=True)
            sleep(delay)
    with stream as events:
        for event in events:
            response = getattr(event, "response", None)
            if response is None:
                continue
            raw = response.model_dump(mode="json")
            if result.get("response_id") not in (None, raw["id"]):
                raise ValueError("Discovery stream changed response identity.")
            result.update(
                response_id=raw["id"], execution=dict(provider_status=raw["status"], model=request["model"])
            )
            write(target / "attempt.json", result)
            write(target / "response.json", raw)
            if raw["status"] in ("completed", "failed", "incomplete", "cancelled"):
                return response
    raise RuntimeError("Discovery stream ended before its terminal receipt.")


def collect(folder, symbol, cutoff, cycle, context, client, proposed=None):
    """Run the foreground search, save its receipt, and retain unsupported findings as leads."""
    request, payload = request_for(symbol, cutoff, cycle, context, proposed)
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / "request.json", request)
    response = streamed_discovery(client, request, folder, {"as_of": cutoff})
    raw = response.model_dump(mode="json")
    value, coverage = validate(raw, response.output_text, payload, SOL_MODEL)
    packet = dict(
        schema="analyst-discovery-v1",
        symbol=symbol,
        as_of=cutoff,
        scope_source=payload["discovery_scope_source"],
        request_sha256=digest(compact(request)),
        watch_sha256=None,
        monitor_state_sha256=None,
        response_id=response.id,
        model=SOL_MODEL,
        received_at=stamp(),
        **value,
        coverage=coverage,
        usage=raw.get("usage"),
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
            raise ValueError("Historical discovery dispositions must reference distinct known findings.")
        items = [item for item in items if item.get("event_id") in ids]
        forecast["discovery_dispositions"] = items
        if history_items:
            forecast["historical_discovery_dispositions"] = history_items
    allowed = {c["id"] for c in forecast.get("monitoring_handoff", {}).get("conditions", [])} | {"UNLISTED"}
    if (
        packet["schema"] != "analyst-discovery-v1"
        or packet["model"] != SOL_MODEL
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


def closing_packet(initial, closing):
    """Keep both sweeps and every finding; analysts must disposition all of them."""
    if (
        initial["symbol"] != closing["symbol"]
        or closing["scope_source"] != "proposed_monitor_handoff"
        or instant(closing["as_of"]) < instant(initial["as_of"])
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
    packet["coverage"]["inherited_gaps"] = initial["coverage"].get("gaps", []) + initial["coverage"].get(
        "inherited_gaps", []
    )
    ids = [e["id"] for e in packet["events"] + packet["coverage"]["unverified_findings"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Closing discovery reused a finding identity.")
    return packet
