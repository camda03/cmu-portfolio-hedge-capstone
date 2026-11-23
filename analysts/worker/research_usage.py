"""Count observed usage and price complete provider receipts at the recorded standard rates."""

from datetime import datetime
import json

from monitor import token_cost

# Standard USD per million tokens, in the order used by monitor.token_cost.
STANDARD = {
    "gpt-6.2-sol": (2, 0.10, 2.50, 5, 10),
    "gpt-6.1-sol": (2, 0.10, 2.50, 5, 10),
    "gpt-6.2-astra": (8, 0.40, 10, 20, 40),
    "gpt-6-astra": (10, 1, 12.50, 25, 50),
}
WEB_SEARCH_USD = 0.01


def rates(model, count):
    family = next(
        (name for name in STANDARD if model == name or (model or "").startswith(name + "-")), None
    )
    if family is None:
        raise ValueError("No standard price for the returned model.")
    base = STANDARD[family]
    return (
        base if count <= 272000 else tuple(v * factor for v, factor in zip(base, (2, 2, 2, 2, 1.5)))
    )


def read_json(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def call_metrics(receipt):
    usage = receipt.get("usage") or {}
    inputs = usage.get("input_tokens_details") or {}
    outputs = usage.get("output_tokens_details") or {}
    included = receipt.get("hosted_tools_enabled")
    search = ((receipt.get("tool_usage") or {}).get("web_search") or {}).get("num_requests")
    if included is False and search is None:
        search = 0
    token_usd = None
    if (
        receipt.get("service_tier") in ("default", None)
        and receipt.get("returned_model")
        and all(type(usage.get(k)) is int for k in ("input_tokens", "output_tokens"))
        and all(type(inputs.get(k)) is int for k in ("cached_tokens", "cache_write_tokens"))
    ):
        try:
            priced = usage
            ttl = receipt.get("requested_cache_ttl")
            if (
                inputs["cache_write_tokens"]
                and not inputs.get("cache_write_tokens_details")
                and ttl in ("30m", "12h")
            ):
                duration = {"ttl_" + ttl + "_tokens": inputs["cache_write_tokens"]}
                priced = dict(
                    usage, input_tokens_details=dict(inputs, cache_write_tokens_details=duration)
                )
            token_usd = token_cost(
                priced, None, rates(receipt["returned_model"], usage["input_tokens"])
            )
        except ValueError:
            pass  # Preserve raw counters for unpriced models and incomplete billing breakdowns.
    search_usd = WEB_SEARCH_USD * search if type(search) is int and search >= 0 else None
    total = token_usd + search_usd if token_usd is not None and search_usd is not None else None
    if total is None and "historical_estimate" not in receipt and included is False:
        total = receipt.get(
            "estimated_usd"
        )  # The original tools-disabled replay recorded these estimates.
    if receipt.get("terminal") == "rate_limited" and not receipt.get("response_id"):
        token_usd = search_usd = total = 0
        search = 0
    started, finished = receipt.get("started_at"), receipt.get("completed_at")
    created, ended = receipt.get("provider_created_at"), receipt.get("provider_completed_at")
    return dict(
        input_tokens=usage.get("input_tokens"),
        cache_read_tokens=inputs.get("cached_tokens"),
        cache_write_tokens=inputs.get("cache_write_tokens"),
        output_tokens=usage.get("output_tokens"),
        reasoning_tokens=outputs.get("reasoning_tokens"),
        billable_web_searches=search,
        worker_call_seconds=(
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds()
        if started and finished
        else None,
        provider_response_seconds=ended - created
        if ended is not None and created is not None
        else None,
        token_estimate_usd=token_usd,
        search_estimate_usd=search_usd,
        estimated_usd=total,
    )


def totals(receipts):
    rows = [call_metrics(r) for r in receipts]
    # A missing reading is unknown. A terminal failure can still have metered usage.
    amounts = {
        k: sum(r[k] for r in rows) if rows and all(r[k] is not None for r in rows) else None
        for k in call_metrics({})
    }
    priced = [row["estimated_usd"] for row in rows if row["estimated_usd"] is not None]
    components = [
        v
        for r in rows
        for v in (r["token_estimate_usd"], r["search_estimate_usd"])
        if v is not None
    ]
    return dict(
        receipts=len(rows),
        completed=sum(r.get("terminal") == "completed" for r in receipts),
        with_usage=sum(bool(r.get("usage")) for r in receipts),
        with_price_estimate=len(priced),
        with_token_price=sum(r["token_estimate_usd"] is not None for r in rows),
        with_search_count=sum(r["billable_web_searches"] is not None for r in rows),
        known_components_usd=sum(components) if components else None,
        known_estimated_usd_subtotal=sum(priced) if priced else None,
        **amounts,
    )


def check_ids(calls):
    ids = [r["response_id"] for r in calls.values() if r.get("response_id")]
    if len(ids) != len(set(ids)):
        raise ValueError(
            "Duplicate provider response ID across calls. Reconcile before aggregating."
        )


def task(folder):
    """Read dispatched calls for one hourly cycle or one analyst run, including both discovery sweeps."""
    calls = {}
    for cycle in [folder, *sorted(folder.glob("*/cycles/*"))]:
        raw, result = (read_json(cycle / name) for name in ("response.json", "result.json"))
        if not raw and not (
            result.get("model_dispatched")
            or read_json(cycle / "attempt.json").get("model_dispatched")
        ):
            continue
        if raw.get("id") and result.get("response_id") and raw["id"] != result["response_id"]:
            raise ValueError("Saved cycle and response IDs disagree at " + str(cycle))
        calls[str(cycle.relative_to(folder))] = dict(
            response_id=raw.get("id") or result.get("response_id"),
            returned_model=raw.get("model"),
            terminal=raw.get("status"),
            usage=raw.get("usage") or result.get("usage"),
            tool_usage=raw.get("tool_usage"),
            provider_created_at=raw.get("created_at"),
            service_tier=raw.get("service_tier"),
            provider_completed_at=raw.get("completed_at"),
            historical_estimate=(result.get("cost") or {}).get("total_estimate_usd"),
        )
    for path in sorted(folder.glob("stage-attempts/*/*.json")):
        r = read_json(path)
        role = path.parent.name
        msg = read_json(folder / (role + ".json"))
        if not r.get("response_id") or r["response_id"] != msg.get("id"):
            msg = {}
        meta = msg.get("response_metadata") or {}
        u = r.get("usage_metadata") or msg.get("usage_metadata") or {}
        # LangChain uses different names for the same provider counters.
        inputs, outputs = u.get("input_token_details") or {}, u.get("output_token_details") or {}
        normalized = dict(
            cached_tokens=inputs.get("cache_read"), cache_write_tokens=inputs.get("cache_creation")
        )
        if type(normalized["cache_write_tokens"]) is int:
            # Historical analyst requests always used the SDK's default 30-minute cache lifetime.
            normalized["cache_write_tokens_details"] = dict(
                ttl_30m_tokens=normalized["cache_write_tokens"], ttl_12h_tokens=0
            )
        calls[role + "/" + path.stem] = dict(
            response_id=r.get("response_id"),
            returned_model=r.get("returned_model") or meta.get("model_name"),
            terminal=r.get("provider_status") or meta.get("status") or r.get("status"),
            started_at=r.get("started_at"),
            completed_at=r.get("finished_at"),
            provider_created_at=r.get("provider_created_at"),
            provider_completed_at=r.get("provider_completed_at"),
            service_tier=r.get("service_tier") or meta.get("service_tier"),
            http_request_id=r.get("http_request_id"),
            hosted_tools_enabled=r.get("hosted_tools_enabled"),
            tool_usage=r.get("tool_usage") or meta.get("tool_usage"),
            usage=r.get("usage")
            or (
                dict(
                    input_tokens=u.get("input_tokens"),
                    output_tokens=u.get("output_tokens"),
                    input_tokens_details=normalized,
                    output_tokens_details=dict(reasoning_tokens=outputs.get("reasoning")),
                )
                if u
                else None
            ),
        )
    if not calls:
        raise ValueError("No saved dispatch attempts in this cycle or analyst run.")
    check_ids(calls)
    return dict(
        recorded_totals=totals(list(calls.values())),
        calls={
            k: dict(
                response_id=r["response_id"],
                http_request_id=r.get("http_request_id"),
                model=r["returned_model"],
                service_tier=r.get("service_tier"),
                status=r["terminal"],
                **call_metrics(r),
            )
            for k, r in calls.items()
        },
    )
