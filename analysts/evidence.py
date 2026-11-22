"""Validate the structure, identity and attribution of news evidence."""

from urllib.parse import parse_qsl, quote_from_bytes, unquote_to_bytes, urlencode, urlsplit
import re


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
        [{k: value[k] for k in ("forecast_id", "baseline_summary", "price_policy", "coverage_gaps")}],
        ("forecast_id", "baseline_summary", "price_policy"),
        ("coverage_gaps",),
    )
    conditions = records(
        value["conditions"],
        ("id", "baseline", "reassess_when", "log_watch_when", "timing"),
        ("aliases", "source_refs"),
    )
    questions = records(value["research_questions"], ("id", "owner", "question", "status"), ("source_refs",))
    ids = [item["id"] for item in conditions + questions]
    if (
        not conditions
        or len(set(ids)) != len(ids)
        or any(not item["source_refs"] for item in conditions + questions)
    ):
        raise ValueError("Require distinct handoff IDs and supporting sources.")
    continuity = records(value["warning_continuity"], ("id", "disposition", "reason"), ("destination_ids",))
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
        raise ValueError("Warning continuity requires distinct IDs and valid explained destinations.")
    return value


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


def continuity_events(forecast, prior_events):
    """Identity lookup only; decision validation still excludes unverified leads."""
    events = {item["event"]["event_key"]: item["event"] for item in discovery_history(forecast)}
    events.update({event["event_key"]: event for event in prior_events})
    return events


def web_url(value):
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("Evidence requires a public HTTP(S) source URL.")
    return value


def trace_sources(raw):
    calls = [x for x in raw.get("output", []) if x.get("type") == "web_search_call"]
    completed = [x.get("action") or {} for x in calls if x.get("status") == "completed"]
    if not any(a.get("type") == "search" for a in completed):
        raise ValueError("No completed hosted search was recorded.")
    urls = {web_url(s["url"]) for a in completed for s in (a.get("sources") or []) if s.get("type") == "url"}
    urls.update(web_url(a["url"]) for a in completed if a.get("url"))
    return urls, calls


def source_trace(raw):
    urls, calls = trace_sources(raw)
    records = {url: ["completed_web_action"] for url in urls}
    for item in raw.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            for annotation in part.get("annotations", []):
                if annotation.get("type") == "url_citation":
                    url = web_url(annotation["url"])
                    records.setdefault(url, []).append("provider_final_url_citation")
    return records, calls


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
            quote_from_bytes(unquote_to_bytes(segment), safe="") for segment in parts.path.split("/")
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


def match_sources(value, traces):
    """Shared attribution gate: exact traced URLs or preserved, unverified leads."""
    normalizations, rejections = [], []
    for item in value["checks"] + value["events"]:
        for index, url in enumerate(item["source_urls"]):
            if url not in traces:
                locator = source_locator(url)
                matches = [u for u in traces if locator is not None and source_locator(u) == locator]
                if len(matches) == 1:
                    item["source_urls"][index] = matches[0]
                    normalizations.append(dict(item_id=item["id"], reported_url=url, provider_url=matches[0]))
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
