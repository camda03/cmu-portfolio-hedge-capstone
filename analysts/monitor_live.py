"""One fresh-news research test; saved evidence and shadow alerts."""

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import count
import math
import random
import fcntl
from html import escape
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
from time import monotonic, sleep
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import market_sessions

from pydantic import BaseModel, ConfigDict
from typing import Literal
from monitor import Decision, action_for, checked_decision, digest, read, stamp, token_cost, write

ROOT = Path(__file__).resolve().parent
from securities import CONTRACTS

MODEL_PROFILE = os.environ.get("CAPSTONE_MODEL_PROFILE", "legacy")
SOL_MODEL, ASTRA_MODEL = {
    "legacy": ("gpt-6.1-sol", "gpt-6-astra"),
    "6.2": ("gpt-6.2-sol", "gpt-6.2-astra"),
}[MODEL_PROFILE]
SETTINGS = dict(
    model=SOL_MODEL,
    reasoning={"effort": "max"},
    max_output_tokens=128000,
    service_tier="default",
    background=True,
    store=False,
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
HTTP_TIMEOUT, POLL_SECONDS = 90, 5
QUEUE_WAIT_SECONDS = 15 * 60  # Initial provider queue only; active research has no local deadline.
SPEND_ERRORS = frozenset(
    (
        "project_spend_limit_exceeded",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "credit_balance_exhausted",
        "insufficient_quota",
    )
)
# Standard short-context USD/M: input, cache read, 30m write, 12h write, output (2026-11-11).
TOKEN_RATES = {
    "gpt-6.2-sol": (2.0, 0.10, 2.50, 5.0, 10.0),
    "gpt-6.1-sol": (2.0, 0.10, 2.50, 5.0, 10.0),
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.25, 0.50),
}
FILES = (
    "monitor_live.py",
    "monitor.py",
    "monitor_live_prompt.md",
    "connect_ibkr.sh",
    "market_sessions.py",
    "securities.py",
    "dashboard/securities.json",
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Check(Strict):
    id: str
    status: Literal["checked", "partial", "failed", "not_checked"]
    source_urls: list[str]
    note: str


class Event(Strict):
    id: str
    event_key: str
    source_urls: list[str]
    published_at: str | None
    publication_label: str
    text: str
    previous_event_id: str | None


class Observation(Strict):
    checks: list[Check]
    events: list[Event]
    decision: Decision


# Encode a monitor record as compact, consistently ordered JSON.
def compact(value):
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=True, allow_nan=False
    )


# Parse an offset-aware timestamp and convert it to UTC.
def instant(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("Timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


# Extract the provider's failure identifiers for the audit record.
def provider_error(raw):
    """Retain machine-readable failure identity, without account URLs or error prose."""
    error = raw.get("error") or {}
    if not isinstance(error, dict):
        return {}
    return {
        key: value
        for key in ("code", "type")
        if isinstance(value := error.get(key), str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value)
    }


# Accept the requested model name or its dated version.
def matches_model(actual, requested):
    return actual == requested or bool(
        isinstance(actual, str)
        and re.fullmatch(re.escape(requested) + r"-\d{4}-\d{2}-\d{2}", actual)
    )


# Identify a recorded provider overload that may safely qualify for a retry.
def confirmed_overload(response, response_id, model):
    error = provider_error(response)
    returned_model = response.get("model")
    return (
        isinstance(response_id, str)
        and re.fullmatch(r"resp[-_A-Za-z0-9]+", response_id) is not None
        and (
            returned_model is None
            or returned_model == model
            or isinstance(returned_model, str)
            and returned_model.startswith(model + "-")
        )
        and response.get("id") == response_id
        and response.get("status") == "failed"
        and error.get("code") == "server_is_overloaded"
        and error.get("type") in (None, "service_unavailable_error")
    )


# Validate the web address before using it as a source.
def web_url(value):
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("Evidence requires a public HTTP(S) source URL.")
    return value


# Check the forecast watch and its condition identifiers before monitoring begins.
def validate_watch(watch):
    ids = [c["id"] for c in watch["conditions"]]
    checks = [c["id"] for c in watch["checks"]]
    if (
        watch["schema"] != "monitor-live-watch-v1"
        or watch["symbol"] not in CONTRACTS
        or not ids
        or len(ids) != len(set(ids))
        or not checks
        or len(checks) != len(set(checks))
        or not any(not c["condition_ids"] for c in watch["checks"])
        or set(ids) != {i for c in watch["checks"] for i in c["condition_ids"]}
        or instant(watch["information_cutoff"]) > instant(watch["issued_at"])
        or instant(watch["issued_at"]) >= instant(watch["valid_until"])
    ):
        raise ValueError("Watch identity, condition coverage or chronology is unsuitable.")
    for check in watch["checks"]:
        for url in check["source_urls"]:
            web_url(url)
    return watch


# Create empty checkpoints and event history tied to this watch's fingerprint.
def initial_state(watch):
    return dict(watch_sha256=digest(compact(watch)), checkpoints={}, prior_events=[], attention={})


# Recover the latest accepted monitor state from saved cycle records.
def recover_state(folder, watch):
    state = (
        read(folder / "state.json") if (folder / "state.json").exists() else initial_state(watch)
    )
    if state["watch_sha256"] != digest(compact(watch)):
        raise ValueError("Saved state belongs to another watch.")
    pending = state.pop("pending_cycle", None)
    if pending:
        terminal = folder / "cycles" / pending / "result.json"
        if not terminal.exists():
            raise ValueError(
                "An interrupted cycle has no terminal result; reconcile it before proceeding."
            )
        result = read(terminal)
        if digest(compact(state)) != result["state_before_sha256"]:
            raise ValueError("State changed during an incomplete commit; review before proceeding.")
        state = result.get("next_state", state)
        if result.get("requires_review"):
            state = dict(state, requires_review=result["requires_review"])
        write(folder / "state.json", state)
    return state


# Build the model request from the active watch, prior state, and evidence cutoff.
def request_for(watch, state, cutoff, cycle, ibkr, model=SETTINGS["model"]):
    if model not in TOKEN_RATES:
        raise ValueError("Unknown research-test model.")
    windows = {
        c["id"]: dict(
            since=max(
                instant(watch["information_cutoff"]),
                instant(state["checkpoints"].get(c["id"], watch["information_cutoff"]))
                - timedelta(hours=2),
            ).isoformat(),
            as_of=cutoff,
        )
        for c in watch["checks"]
    }
    forecast = json.loads(compact(watch))
    for check in forecast["checks"]:
        check["navigation_targets"] = check.pop("source_urls")
    prior_events = json.loads(compact(state["prior_events"]))
    for event in prior_events:
        event["navigation_targets"] = event.pop("source_urls")
    payload = dict(
        forecast=forecast,
        requested_windows=windows,
        as_of=cutoff,
        as_of_new_york=instant(cutoff).astimezone(ZoneInfo("America/New_York")).isoformat(),
        event_id_prefix=digest(cycle)[:8] + "-",
        prior_events=prior_events,
        prior_attention=state["attention"],
        ibkr=ibkr,
    )
    request = dict(
        SETTINGS,
        model=model,
        instructions=(ROOT / "monitor_live_prompt.md").read_text(),
        input=compact(payload),
        text={
            "format": {
                "type": "json_schema",
                "name": "live_monitor",
                "strict": True,
                "schema": Observation.model_json_schema(),
            }
        },
    )
    return request, payload


# Freeze the live monitor's watch and configuration in a new work directory.
def prepare(watch_path, folder, model=SETTINGS["model"]):
    watch = validate_watch(read(watch_path))
    source = (ROOT / watch["source_report_path"]).resolve()
    if not source.is_relative_to(ROOT):
        raise ValueError("The report source must be inside this project.")
    report = read(source)["original_report"]["text"]
    if digest(report) != watch["source_report_sha256"]:
        raise ValueError("Original report fingerprint mismatch.")
    provenance = read(watch_path.with_name("provenance.json"))
    if provenance["prepared_watch_sha256"] != digest(watch_path.read_bytes()) or any(
        item["excerpt"] not in report for item in provenance["source_item_reconciliation"]
    ):
        raise ValueError("Watch provenance or original-report excerpts do not match.")
    files = {name: (ROOT / name).read_bytes() for name in FILES}
    files.update(
        {
            "watch.json": watch_path.read_bytes(),
            "original-report.md": report.encode(),
            "watch-provenance.json": watch_path.with_name("provenance.json").read_bytes(),
        }
    )
    hashes = {name: digest(data) for name, data in files.items()}
    manifest = dict(files=hashes, model=model)
    manifest["fingerprint"] = digest(compact(manifest))
    request, _ = request_for(
        watch, initial_state(watch), stamp(), "smoke-1", {"status": "not_collected"}, model
    )
    packages = {name: version(name) for name in ("openai", "pydantic")}
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "manifest.json").exists():
        if read(folder / "manifest.json") != manifest:
            raise ValueError(
                "This session has different frozen inputs; preserve it and use a new session."
            )
        verify(folder)
        return
    for name, data in files.items():
        target = folder / "frozen" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    write(folder / "manifest.json", manifest)
    write(
        folder / "preflight.json",
        dict(
            status="prepared_not_run",
            model=model,
            request_bytes_without_snapshot=len(compact(request).encode()),
            max_request_bytes=None,
            model_generations_at_most=1,
            max_tool_calls=None,
            max_events=None,
            max_saved_stories=None,
            max_output_tokens=SETTINGS["max_output_tokens"],
            reasoning=SETTINGS["reasoning"],
            web_search=SETTINGS["tools"][0],
            background=True,
            store=False,
            http_timeout_seconds=HTTP_TIMEOUT,
            poll_seconds=POLL_SECONDS,
            research_deadline_seconds=None,
            automatic_retries=0,
            status_read_retries=2,
            billing_cap_usd=None,
            token_rates_usd_per_million=dict(
                zip(("input", "cache_read", "write_30m", "write_12h", "output"), TOKEN_RATES[model])
            ),
            cost_note="No local research or spending cap. Provider limits still apply. Search fees and retrieved-content tokens are billable; unknown usage requires reconciliation.",
            pricing_url="https://developers.openai.com/api/docs/pricing",
            shadow_only=True,
        ),
    )
    write(folder / "packages.json", packages)
    render(folder, {"status": "prepared_not_run", "symbol": watch["symbol"]})
    print(
        f"Prepared one {model} research generation; max reasoning and 128,000 output tokens.\n"
        "No local tool-count, research-time or spending cap; provider limits apply; zero automatic retries.",
        flush=True,
    )


# Recheck the frozen files and load the watch saved for this monitor.
def verify(folder):
    manifest = read(folder / "manifest.json")
    if (
        manifest["model"] not in TOKEN_RATES
        or digest(compact({k: manifest[k] for k in ("files", "model")})) != manifest["fingerprint"]
    ):
        raise ValueError("Manifest fingerprint mismatch.")
    for name, expected in manifest["files"].items():
        if digest((folder / "frozen" / name).read_bytes()) != expected:
            raise ValueError("Frozen input changed: " + name)
    for name in FILES:
        if digest((ROOT / name).read_bytes()) != manifest["files"][name]:
            raise ValueError("Use the frozen runner and matching dependencies.")
    return validate_watch(read(folder / "frozen" / "watch.json"))


# Collect the instrument's broker observations and record any collection failure.
def collect_ibkr(folder, watch, *, now=None, cache_dir=None):
    clock = instant(now or stamp())
    request = dict(
        contract_id=CONTRACTS[watch["symbol"]],
        market_data_names=[
            "last",
            "top_status",
            "bid_ask",
            "prior_close",
            "volume",
            "misc_statistics",
            "historical_vol",
            "implied_vol_underlying",
        ],
    )
    write(folder / "snapshot-request.json", request)
    target = folder / "snapshot-response.json"
    record = dict(
        provider="IBKR", symbol=watch["symbol"], tool="get_price_snapshot", request=request
    )
    try:
        record["quote_session"] = market_sessions.quote_session(watch["symbol"], clock)
    except (KeyError, ValueError, IndexError, RuntimeError):
        # Do not guess either opening hours or paid feed access; preserve the gap.
        record["quote_session"] = dict(
            state="unknown",
            fetch_quote=False,
            skip_reason="calendar_unavailable",
            checked_at=clock.isoformat(),
        )
    cache_path = (cache_dir or folder.parents[1] / "market-data") / (
        watch["symbol"] + "-snapshot.json"
    )
    cached = None
    if cache_path.exists():
        try:
            if cache_path.stat().st_size > 32768:
                raise ValueError("Saved snapshot is oversized.")
            cached = read(cache_path)
            if (
                cached["provider"] != "IBKR"
                or cached["symbol"] != watch["symbol"]
                or cached["tool"] != record["tool"]
                or cached["request"] != request
                or cached["status"] != "received"
                or not isinstance(cached["response"], dict)
                or cached["response"].get("isError")
                or instant(cached["retrieved_at"]) > clock
            ):
                raise ValueError("Saved snapshot identity or chronology mismatch.")
        except (OSError, KeyError, TypeError, ValueError):
            cached = None
            record["cache_issue"] = "saved_snapshot_invalid"
    if not record["quote_session"]["fetch_quote"]:
        return dict(
            record,
            status=record["quote_session"]["skip_reason"],
            retained_snapshot=cached,
            note="No quote request. See session/access status; saved timestamps are unchanged.",
        )
    record["retained_snapshot"] = cached
    try:
        # Connector allows 900s for the shared OAuth lock, then 90s for broker I/O.
        done = subprocess.run(
            [
                "timeout",
                "--kill-after=5s",
                "1000s",
                "bash",
                str(ROOT / "connect_ibkr.sh"),
                "snapshot",
                compact(request),
                str(target),
            ],
            capture_output=True,
            timeout=1010,
        )
        if done.returncode or not target.is_file() or target.stat().st_size > 16384:
            return dict(
                record,
                status="unavailable",
                exit_code=done.returncode,
                error_code={
                    75: "local_lock_timeout",
                    124: "client_timeout",
                    137: "client_timeout",
                }.get(
                    done.returncode,
                    "broker_client_failed" if done.returncode else "invalid_response",
                ),
            )
        response = read(target)
        if not isinstance(response, dict):
            raise ValueError("Malformed IBKR response.")
        retrieved = target.with_suffix(".retrieved_at.txt").read_text().strip()
        if instant(retrieved) > instant(stamp()):
            raise ValueError("IBKR receipt time is in the future.")
        result = dict(
            record,
            status="error" if response.get("isError") else "received",
            retrieved_at=retrieved,
            response=response,
        )
        if result["status"] == "received":
            # Do not recursively accumulate prior snapshots or credentials in the cache.
            try:
                write(cache_path, {k: v for k, v in result.items() if k != "retained_snapshot"})
            except OSError:
                result["cache_issue"] = "snapshot_cache_write_failed"
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return dict(record, status="unavailable", error_type=type(error).__name__)


# Extract the URLs and search calls that support the model's reported findings.
def trace_sources(raw):
    calls = [x for x in raw.get("output", []) if x.get("type") == "web_search_call"]
    completed = [x.get("action") or {} for x in calls if x.get("status") == "completed"]
    if not any(a.get("type") == "search" for a in completed):
        raise ValueError("No completed hosted search was recorded.")
    urls = {
        web_url(s["url"])
        for a in completed
        for s in (a.get("sources") or [])
        if s.get("type") == "url"
    }
    urls.update(web_url(a["url"]) for a in completed if a.get("url"))
    return urls, calls


# Validate the response, its evidence, and the proposed next action for this cycle.
def validate_observation(raw, text, watch, state, payload, model=SETTINGS["model"]):
    returned_model = raw.get("model") or ""
    if (returned_model != model and not returned_model.startswith(model + "-")) or raw.get(
        "service_tier"
    ) not in ("default", None):
        raise ValueError(
            "Unexpected returned model or service tier; inspect saved usage and pricing."
        )
    if raw.get("status") != "completed":
        reason = (raw.get("incomplete_details") or {}).get("reason")
        reason = (
            reason
            if reason in ("max_output_tokens", "max_messages", "content_filter", "steered")
            else "unspecified"
        )
        raise ValueError("Provider response did not complete: " + reason + ".")
    value = Observation.model_validate_json(text).model_dump()
    urls, calls = trace_sources(raw)
    expected = {c["id"] for c in watch["checks"]}
    if len(value["checks"]) != len(expected) or {c["id"] for c in value["checks"]} != expected:
        raise ValueError("Required check IDs do not match.")
    rejected = []
    for check in value["checks"]:
        missing = sorted(set(check["source_urls"]) - urls)
        if missing or (check["status"] == "checked" and not check["source_urls"]):
            reason = (
                "Untraced coverage links excluded"
                if missing
                else "Claimed completed check has no traced sources"
            )
            rejected.append(dict(check_id=check["id"], rejected_urls=missing, reason=reason))
            check["source_urls"] = [url for url in check["source_urls"] if url in urls]
            if check["status"] == "checked":
                check["status"] = "partial"
    for item in value["events"]:
        missing = set(item["source_urls"]) - urls
        if missing:
            raise ValueError(
                "Untraced source URL in " + item["id"] + ": " + ", ".join(sorted(missing))
            )
    prior = {e["event_key"]: e for e in state["prior_events"]}
    all_ids = {e["id"] for e in state["prior_events"]}
    keys = set()
    for event in value["events"]:
        old = prior.get(event["event_key"])
        if (
            not event["id"].startswith(payload["event_id_prefix"])
            or event["id"] in all_ids
            or not event["event_key"].strip()
            or event["event_key"] in keys
            or not event["text"].strip()
            or not event["source_urls"]
            or event["previous_event_id"] != (old["id"] if old else None)
        ):
            raise ValueError("Evidence identity, source or prior-version reference is invalid.")
        if event["published_at"] is not None and instant(event["published_at"]) > instant(
            payload["as_of"]
        ):
            raise ValueError("Evidence publication is after the requested cutoff.")
        all_ids.add(event["id"])
        keys.add(event["event_key"])
    gaps = list(watch["data_gaps"]) + [
        c["id"] + ": " + c["status"] for c in value["checks"] if c["status"] != "checked"
    ]
    gaps.extend(r["check_id"] + ": " + r["reason"] for r in rejected)
    if payload["ibkr"].get("status") not in ("received", "market_closed"):
        gaps.append("IBKR snapshot unavailable; public-news collection proceeded independently.")
    elif payload["ibkr"]["status"] == "market_closed" and not payload["ibkr"].get(
        "retained_snapshot"
    ):
        gaps.append(
            "Configured quote feed closed; no saved snapshot. Dated forecast reference retained."
        )
    packet = dict(
        symbol=watch["symbol"],
        as_of=payload["as_of"],
        prior_events=state["prior_events"],
        events=value["events"],
        coverage=dict(
            status="incomplete" if gaps else "complete",
            checked_through=payload["as_of"],
            gaps=gaps,
            source_rejections=rejected,
        ),
    )
    decision = checked_decision(compact(value["decision"]), watch, packet)
    return value, action_for(decision, packet), packet, calls


# Fingerprint an event's source and version details so duplicate evidence can be recognized.
def evidence_key(event):
    return digest(
        compact(
            dict(
                event_key=event["event_key"],
                source_urls=sorted(set(event["source_urls"])),
                published_at=event["published_at"],
                text=event["text"],
            )
        )
    )


# Apply accepted events to a copy of the state and assemble the next handoff.
def advance(state, value, handoff, cutoff):
    result = json.loads(compact(state))
    events = {e["event_key"]: e for e in state["prior_events"]}
    events.update({e["event_key"]: e for e in value["events"]})
    by_id = {e["id"]: e for e in state["prior_events"] + value["events"]}
    decision = value["decision"]
    group = compact([handoff["action"], sorted(set(decision["condition_ids"]))])
    signature = digest(
        compact(
            [
                group,
                sorted({evidence_key(by_id[i]) for i in decision["evidence_ids"]}),
                sorted(set(handoff["coverage_gaps"])),
            ]
        )
    )
    attention = handoff["action"] != "keep_forecast"
    new = attention and state["attention"].get(group, {}).get("signature") != signature
    if attention:
        result["attention"][group] = dict(
            signature=signature,
            reason=decision["reason"],
            verification_question=decision["verification_question"],
            event_keys=sorted({by_id[i]["event_key"] for i in decision["evidence_ids"]}),
        )
    result["prior_events"] = list(events.values())
    for check in value["checks"]:
        if check["status"] == "checked":
            result["checkpoints"][check["id"]] = cutoff
    result["last_completed_cycle_at"] = cutoff
    return result, dict(new=new, type=handoff["action"], execute=False)


# Write a readable HTML account of the completed monitoring cycle.
def render(folder, result):
    # Escape a value and display it as preformatted text in the HTML summary.
    def pre(value):
        return (
            "<pre>"
            + escape(
                value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
            )
            + "</pre>"
        )

    observation = result.get("observation", {})
    body = "<h1>Fresh monitoring review — " + escape(result.get("symbol", "")) + "</h1>"
    body += "<p>Status: <strong>" + escape(result["status"]) + "</strong></p>"
    body += "<p>Shadow test: saved attention requests; no forecast rerun, trade or external notification.</p>"
    for title, item in [
        ("Decision", observation.get("decision")),
        ("Effective action and gaps", result.get("handoff")),
        ("New attention", result.get("alert")),
        ("Requested cutoff", result.get("as_of")),
        ("Provider execution", result.get("execution")),
        ("Usage and estimate", result.get("cost")),
        (
            "Rejected coverage references — excluded from accepted sources",
            result.get("coverage", {}).get("source_rejections"),
        ),
        ("Validation failure", result.get("validation_error")),
        ("Unaccepted model draft — not used for state or alerts", result.get("unaccepted_output")),
    ]:
        if item is not None:
            body += "<h2>" + title + "</h2>" + pre(item)
    for check in observation.get("checks", []):
        body += (
            "<h3>"
            + escape(check["id"] + " — " + check["status"])
            + "</h3><p>"
            + escape(check["note"])
            + "</p>"
        )
        body += "".join(
            '<p><a href="' + escape(web_url(u), quote=True) + '">' + escape(u) + "</a></p>"
            for u in check["source_urls"]
        )
    for event in observation.get("events", []):
        body += (
            "<h3>"
            + escape(event["id"] + " — " + event["event_key"])
            + "</h3><p>"
            + escape(event["text"])
            + "</p>"
        )
        body += (
            "<p>Publication: "
            + escape(event["published_at"] or event["publication_label"] or "Unknown")
            + "</p>"
        )
        body += "".join(
            '<p><a href="' + escape(web_url(u), quote=True) + '">' + escape(u) + "</a></p>"
            for u in event["source_urls"]
        )
    body += "<details><summary>Complete saved result</summary>" + pre(result) + "</details>"
    html = (
        '<!doctype html><meta charset="utf-8"><title>Fresh monitor review</title><style>body{max-width:1100px;margin:2em auto;padding:0 1em;font:17px/1.5 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:1em}a{overflow-wrap:anywhere}h2{border-top:1px solid #ccd;padding-top:1em}</style>'
        + body
    )
    write(folder / "summary.html", html)


# Calculate a retry delay for an explicitly temporary provider refusal.
def rate_delay(error, number):
    """A retry is safe only after an explicit temporary provider refusal."""
    from openai import RateLimitError

    code = getattr(error, "code", None)
    message = str(error).lower()
    if (
        not isinstance(error, RateLimitError)
        or error.status_code != 429
        or "request too large" in message
        or not (
            code in ("rate_limit_exceeded", "slow_down")
            or (
                code is None
                and "rate limit reached" in message
                and ("tokens per min" in message or "requests per min" in message)
            )
        )
    ):
        return None
    delay = 30 * 2 ** min(2, number)
    hint = error.response.headers.get("retry-after")
    if hint:
        try:
            try:
                server_delay = float(hint)
            except ValueError:
                server_delay = (
                    parsedate_to_datetime(hint) - datetime.now(timezone.utc)
                ).total_seconds()
            if math.isfinite(server_delay):
                delay = max(delay, server_delay)
        except (ValueError, TypeError, OverflowError):
            pass
    return delay + random.uniform(0, 3)


# Stream a discovery request and record its response identifier as soon as it arrives.
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
                response_id=raw["id"],
                execution=dict(provider_status=raw["status"], model=request["model"]),
            )
            write(target / "attempt.json", result)
            write(target / "response.json", raw)
            if raw["status"] in ("completed", "failed", "incomplete", "cancelled"):
                return response
    raise RuntimeError("Discovery stream ended before its terminal receipt.")


# Poll the existing provider response by its saved identifier.
def retrieve_response(client, response_id):
    """Retry only an idempotent status read; never resubmit the research request."""
    return client.with_options(max_retries=2).responses.retrieve(
        response_id, include=SETTINGS["include"]
    )


# Validate and account for a completed response before saving the cycle outcome.
def complete_response(result, response, watch, state, payload, model, validator, advancer):
    """The same acceptance and cost checks serve fresh and recovered responses."""
    raw = response.model_dump(mode="json")
    result["received_at"] = stamp()
    result["usage"] = raw.get("usage")
    result["execution"] = dict(provider_status=raw.get("status"), model=raw.get("model"))
    result["phase"] = "response_validation"
    result["unaccepted_output"] = response.output_text
    calls = [x for x in raw.get("output", []) if x.get("type") == "web_search_call"]
    search_count = sum((c.get("action") or {}).get("type") == "search" for c in calls)
    reported = (raw.get("tool_usage") or {}).get("web_search") or {}
    search_requests = reported.get("num_requests", search_count)
    result.update(
        tool_calls=len(calls),
        search_actions=search_count,
        execution=dict(
            provider_status=raw.get("status"),
            model=raw.get("model"),
            tool_calls=len(calls),
            search_actions=search_count,
            search_requests=search_requests,
            missing_tool_actions=[c.get("id") for c in calls if not c.get("action")],
        ),
    )
    try:
        if raw.get("model") != model and not (raw.get("model") or "").startswith(model + "-"):
            raise ValueError("Unexpected model pricing.")
        if (
            raw.get("service_tier") not in ("default", None)
            or type(search_requests) is not int
            or search_requests < 0
        ):
            raise ValueError("Unexpected service tier or search usage.")
        long_context = raw["usage"]["input_tokens"] > 272000
        rates = tuple(
            rate * factor
            for rate, factor in zip(
                TOKEN_RATES[model], (2, 2, 2, 2, 1.5) if long_context else (1, 1, 1, 1, 1)
            )
        )
        tokens = token_cost(raw["usage"], "coached", rates=rates)
        result["cost"] = dict(
            token_estimate_usd=tokens,
            search_fee_estimate_usd=search_requests * 0.01,
            total_estimate_usd=tokens + search_requests * 0.01,
            context_rate_basis="long" if long_context else "short",
            search_count_basis="provider tool_usage"
            if "num_requests" in reported
            else "observed search actions; provider count absent",
            note="Conservative saved-rate estimate selected from aggregate input usage; actual billed context tier/mix is unverified. "
            "Unknown cache-write duration uses the higher saved write rate. Not an invoice or hard billing cap.",
        )
    except (KeyError, TypeError, ValueError):
        result["cost"] = {
            "note": "Returned usage needs manual cost reconciliation; raw response retained."
        }
    if raw.get("status") == "failed":
        result.update(phase="provider_failure", provider_error=provider_error(raw))
        raise RuntimeError("Provider returned a failed response; saved draft remains unaccepted.")
    value, handoff, packet, _ = validator(raw, response.output_text, watch, state, payload, model)
    next_state, alert = advancer(state, value, handoff, result["as_of"])
    result.pop("unaccepted_output")
    result.update(
        status="completed",
        observation=value,
        handoff=handoff,
        alert=alert,
        coverage=packet["coverage"],
        next_state=next_state,
        phase="state_publication",
    )
    return result


# Recover a known response after polling was interrupted.
def reconcile_cycle(
    folder,
    client,
    *,
    verifier=verify,
    requester=request_for,
    validator=validate_observation,
    advancer=advance,
    renderer=render,
):
    """Read a known response after polling failed; never create or cancel a request."""
    watch = verifier(folder)
    model = read(folder / "manifest.json")["model"]
    with (folder / ".run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = recover_state(folder, watch)
        hold = state.get("requires_review")
        if not hold:
            return {"status": "nothing_to_recover", "model_requests": 0}
        cycle, response_id = hold.get("cycle"), hold.get("response_id")
        if (
            not isinstance(cycle, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cycle)
            or not isinstance(response_id, str)
            or not re.fullmatch(r"resp[-_A-Za-z0-9]+", response_id)
        ):
            raise ValueError("Recovery requires a saved cycle and response identity.")
        target = folder / "cycles" / cycle
        prior, attempt, raw = (
            read(target / name) for name in ("result.json", "attempt.json", "response.json")
        )
        before = {k: v for k, v in state.items() if k != "requires_review"}
        if (
            prior.get("phase") != "model_polling"
            or prior.get("status") != "failed"
            or prior.get("error_type")
            not in (
                "APIConnectionError",
                "APITimeoutError",
                "InternalServerError",
                "RateLimitError",
            )
            or prior.get("model_dispatched") is not True
            or prior.get("requires_review") != hold
            or raw.get("status") not in ("queued", "in_progress")
            or any(item.get("response_id") != response_id for item in (prior, attempt))
            or raw.get("id") != response_id
            or prior["state_before_sha256"] != digest(compact(before))
            or attempt["state_before_sha256"] != prior["state_before_sha256"]
        ):
            raise ValueError(
                "Only a bound status-read failure can be recovered without resubmission."
            )
        if (target / "state-before.json").exists() and read(target / "state-before.json") != before:
            raise ValueError("Saved state changed before recovery.")
        request, payload = requester(
            watch, before, prior["as_of"], cycle, read(target / "ibkr-context.json"), model
        )
        if request != read(target / "request.json"):
            raise ValueError("Saved request differs from its frozen inputs.")
        receipt = dict(
            status="retrieval_started",
            response_id=response_id,
            checked_at=stamp(),
            model_requests=0,
            broker_requests=0,
            original_result_sha256=digest((target / "result.json").read_bytes()),
        )
        try:
            response = retrieve_response(client, response_id)
            fetched = response.model_dump(mode="json")
            if fetched.get("id") != response_id:
                raise ValueError("Retrieval returned a different response identity.")
            write(target / "polling-response.json", fetched)
            receipt["provider_status"] = fetched.get("status")
            if fetched.get("status") in ("queued", "in_progress"):
                receipt["status"] = "response_pending"
                write(target / "polling-recovery.json", receipt)
                return receipt
            result = {
                k: prior[k]
                for k in (
                    "cycle",
                    "symbol",
                    "as_of",
                    "model_dispatched",
                    "state_before_sha256",
                    "response_id",
                )
            }
            try:
                complete_response(
                    result, response, watch, before, payload, model, validator, advancer
                )
            except RuntimeError:
                if result.get("phase") != "provider_failure" or not confirmed_overload(
                    fetched, response_id, model
                ):
                    raise
                result.update(
                    status="failed", error_type="RuntimeError", note="Confirmed provider overload."
                )
        except Exception as error:
            receipt.update(
                status="recovery_requires_review",
                error_type=type(error).__name__,
                http_status=getattr(error, "status_code", None),
            )
            write(target / "polling-recovery.json", receipt)
            return receipt
        # Preserve the original failure, then use the existing pending-cycle commit protocol.
        for name in ("result.json", "response.json"):
            backup = target / ("polling-original-" + name)
            if backup.exists() and backup.read_bytes() != (target / name).read_bytes():
                raise ValueError("Original polling evidence changed.")
            if not backup.exists():
                backup.write_bytes((target / name).read_bytes())
        receipt["status"] = "response_reconciled"
        result["polling_recovery"] = receipt
        write(folder / "state.json", dict(before, pending_cycle=cycle))
        write(target / "result.json", result)
        write(folder / "state.json", result.get("next_state", before))
        write(target / "response.json", fetched)
        renderer(target, result)
        renderer(folder, result)
        write(target / "polling-recovery.json", receipt)
        return receipt


# Run one monitoring cycle under its saved watch and persist the outcome.
def run_cycle(
    folder,
    cycle,
    client=None,
    broker=False,
    now=None,
    *,
    verifier=verify,
    requester=request_for,
    validator=validate_observation,
    advancer=advance,
    renderer=render,
    broker_cache=None,
):
    watch = verifier(folder)
    model = read(folder / "manifest.json")["model"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cycle):
        raise ValueError("Use a short alphanumeric cycle ID.")
    with (folder / ".run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = recover_state(folder, watch)
        target = folder / "cycles" / cycle
        if (target / "attempt.json").exists():
            print("This cycle was already attempted; no new broker or model request.", flush=True)
            result = (
                read(target / "result.json")
                if (target / "result.json").exists()
                else {"status": "uncertain"}
            )
            renderer(target, result)
            renderer(folder, result)
            return result
        start = now or stamp()
        if not instant(watch["issued_at"]) <= instant(start) < instant(watch["valid_until"]):
            raise ValueError("Watch is not active at this cycle time.")
        if state["watch_sha256"] != digest(compact(watch)) or state.get("requires_review"):
            raise ValueError("Saved state requires review or belongs to another watch.")
        if any(instant(t) > instant(start) for t in state["checkpoints"].values()):
            raise ValueError("Cycle time precedes a saved successful check.")
        requester(watch, state, start, cycle, {"status": "not_collected"}, model)
        target.mkdir(parents=True, exist_ok=True)
        write(target / "attempt.json", dict(cycle=cycle, started_at=start, model_dispatched=False))
        write(folder / "state.json", dict(state, pending_cycle=cycle))
        result = dict(
            status="attempted",
            cycle=cycle,
            symbol=watch["symbol"],
            as_of=start,
            model_dispatched=False,
            state_before_sha256=digest(compact(state)),
            phase="broker_collection",
        )
        raw = {}
        try:
            ibkr = (
                collect_ibkr(target, watch, now=now, cache_dir=broker_cache)
                if broker
                else {"status": "not_requested"}
            )
            write(target / "ibkr-context.json", ibkr)
            cutoff = now or stamp()
            if instant(cutoff) >= instant(watch["valid_until"]):
                raise ValueError("Watch expired during collection.")
            request, payload = requester(watch, state, cutoff, cycle, ibkr, model)
            result["as_of"] = cutoff
            write(target / "request.json", request)
            if client is None:
                from openai import OpenAI

                if not os.environ.get("OPENAI_API_KEY"):
                    raise ValueError("Approved capstone API credential unavailable.")
                client = OpenAI(
                    max_retries=0, timeout=7200 if request.get("stream") else HTTP_TIMEOUT
                )
            result["model_dispatched"] = True
            result["phase"] = "model_request"
            write(target / "attempt.json", result)
            if request.get("stream"):
                print(f"Calling {model} for foreground discovery with live search.", flush=True)
                try:
                    response = streamed_discovery(client, request, target, result)
                except Exception:
                    if not result.get("response_id"):
                        raise
                    # Reconcile the same stored response if the client stream is interrupted.
                    response = retrieve_response(client, result["response_id"])
                    if response.id != result["response_id"]:
                        raise ValueError("Stream recovery returned a different response identity.")
            else:
                print(
                    f"Calling {model} once in background with live search; shadow decision only.",
                    flush=True,
                )
                response = client.responses.create(**request)
            raw = response.model_dump(mode="json")
            result["response_id"] = raw.get("id")
            write(target / "response.json", raw)
            queued_at, progressed = monotonic(), False
            while raw.get("status") in ("queued", "in_progress"):
                progressed = progressed or raw["status"] == "in_progress"
                result.update(
                    phase="model_polling",
                    execution=dict(provider_status=raw["status"], model=model),
                )
                write(target / "attempt.json", result)
                if request.get("stream") and monotonic() - queued_at >= 7200:
                    raise TimeoutError(
                        "Foreground discovery recovery is still pending. Its ID is saved."
                    )
                if (
                    not request.get("stream")
                    and not progressed
                    and monotonic() - queued_at >= QUEUE_WAIT_SECONDS
                ):
                    # The last retrieval still says queued. Cancellation can race completion;
                    # validate the returned terminal response rather than assuming cancellation.
                    result.update(phase="queue_cancellation", queue_wait_exceeded=True)
                    write(target / "attempt.json", result)
                    response = client.responses.cancel(raw["id"])
                    raw = response.model_dump(mode="json")
                    if raw.get("id") != result["response_id"]:
                        raw = {}
                        raise ValueError("Cancellation returned a different response identity.")
                    write(target / "response.json", raw)
                    if raw.get("status") not in ("completed", "failed", "cancelled", "incomplete"):
                        raise TimeoutError(
                            "Cancellation is not confirmed; hold this attempt for review."
                        )
                    break
                print(
                    "Provider status: "
                    + raw["status"]
                    + "; observing the same research generation.",
                    flush=True,
                )
                sleep(POLL_SECONDS)
                response = retrieve_response(client, raw["id"])
                raw = response.model_dump(mode="json")
                if raw.get("id") != result["response_id"]:
                    raw = {}
                    raise ValueError("Retrieval returned a different response identity.")
                write(target / "response.json", raw)
            complete_response(result, response, watch, state, payload, model, validator, advancer)
        except Exception as error:
            result.update(
                status="failed",
                error_type=type(error).__name__,
                http_status=getattr(error, "status_code", None),
                note="No automatic retry. Inspect saved inputs/raw response; this attempt remains consumed.",
            )
            details = provider_error(
                {
                    "error": {
                        "code": getattr(error, "code", None),
                        "type": getattr(error, "type", None),
                    }
                }
            )
            if details:
                result.update(phase="provider_failure", provider_error=details)
            rejected = (
                not result.get("response_id")
                and result.get("http_status") == 429
                and details.get("code") in SPEND_ERRORS
            )
            if (
                result["model_dispatched"]
                and not rejected
                and raw.get("status") not in ("completed", "failed", "cancelled", "incomplete")
            ):
                result["requires_review"] = dict(
                    cycle=cycle,
                    response_id=result.get("response_id"),
                    reason="Provider outcome unconfirmed; reconcile this attempt before starting another cycle.",
                )
            if result["model_dispatched"] and "cost" not in result:
                result["cost"] = {
                    "note": "Request rejected by the provider spending limit; no usage returned."
                    if rejected
                    else "Saved response needs cost reconciliation."
                    if "received_at" in result
                    else "Final usage and cost unknown; the provider may still be running. Do not repeat generation."
                }
            if result["phase"] == "response_validation" and type(error) is ValueError:
                result["validation_error"] = str(error)
        write(target / "result.json", result)
        if result.get("requires_review"):
            state = dict(state, requires_review=result["requires_review"])
        write(folder / "state.json", result.get("next_state", state))
        renderer(target, result)
        renderer(folder, result)
        print("Cycle " + result["status"] + "; HTML: " + str(folder / "summary.html"), flush=True)
        if result.get("alert", {}).get("new"):
            print(
                "ATTENTION: "
                + result["alert"]["type"]
                + " — see the saved decision and coverage gaps.",
                flush=True,
            )
        return result


# Run the requested preparation, verification, or monitoring command.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch", type=Path)
    parser.add_argument("--cycle", default="smoke-1")
    parser.add_argument("--ibkr", action="store_true")
    parser.add_argument(
        "--model",
        choices=tuple(TOKEN_RATES),
        default=SETTINGS["model"],
        help="prepare only; run uses the frozen manifest model",
    )
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            if not args.watch:
                parser.error("prepare requires --watch")
            prepare(args.watch.resolve(), args.output.resolve(), args.model)
        elif (
            run_cycle(args.output.resolve(), args.cycle, broker=args.ibkr)["status"] != "completed"
        ):
            raise SystemExit(1)
    except (OSError, ValueError, KeyError) as error:
        print(
            "Preflight failed: "
            + type(error).__name__
            + ". No automatic retry; inspect the saved inputs."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
