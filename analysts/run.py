"""One daily, seven-role assessment for a stock or the S&P 500 cash index."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import count
import json
import math
from pathlib import Path
import re
from time import monotonic, sleep

from .common import (
    CONTRACTS as IBKR_CONTRACTS,
    MODELS,
    ROOT,
    SECURITIES,
    STAGES,
    SUBJECTS,
    digest,
    dump,
    matches_model,
    rate_delay,
    stamp,
    write,
)
from .discovery import GUIDANCE, attach, collect, closing_packet
from .evidence import handoff_structure


def invoke_paced(call, folder, name, timeout):
    """Retry explicit temporary 429s only; retain request receipts and one total deadline."""
    deadline = monotonic() + timeout
    for number in count():
        receipt = folder / "stage-attempts" / name / (str(number + 1) + ".json")
        if receipt.exists():
            raise ValueError("Saved stage attempt requires review: " + name)
        record = dict(started_at=stamp(), status="started", attempt=number + 1)
        write(receipt, record)
        try:
            message = call(max(0.001, deadline - monotonic()))
        except Exception as error:
            code = getattr(error, "code", None)
            delay = rate_delay(error, number)
            temporary = delay is not None
            record.update(
                status="rate_limited" if temporary else "requires_review",
                error_type=type(error).__name__,
                http_status=getattr(error, "status_code", None),
                error_code=code,
                request_id=getattr(error, "request_id", None),
                finished_at=stamp(),
            )
            write(receipt, record)
            if not temporary:
                raise
            if delay >= deadline - monotonic():
                raise  # Defer rather than violate the server delay or total deadline.
            print(f"{name}: temporary token/request rate limit; retrying after {delay:.1f}s", flush=True)
            sleep(delay)
        else:
            meta = getattr(message, "response_metadata", {}) or {}
            record.update(
                status="response_received",
                response_id=getattr(message, "id", None),
                finished_at=stamp(),
                returned_model=meta.get("model_name"),
                provider_status=meta.get("status"),
                service_tier=meta.get("service_tier"),
                tool_usage=meta.get("tool_usage"),
                usage_metadata=getattr(message, "usage_metadata", None),
            )
            write(receipt, record)
            return message


def readable(message):
    """Render prose citations without changing the fenced forecast JSON."""
    parts = []
    for block in message.content_blocks:
        if block["type"] != "text":
            continue
        text, citations = block["text"], {}
        json_spans = [m.span() for m in re.finditer(r"```json\s*.*?```", text, re.S)]
        for item in block.get("annotations", []):
            if item.get("url", "").startswith(("https://", "http://")):
                end = item.get("end_index", len(text))
                if any(start <= end <= stop for start, stop in json_spans):
                    continue
                url = item["url"].replace("(", "%28").replace(")", "%29")
                citations.setdefault(end, []).append(f"[source](<{url}>)")
        for end, links in sorted(citations.items(), reverse=True):
            text = text[:end] + " " + " ".join(links) + text[end:]
        parts.append(
            re.sub(
                r"```json\s*.*?```|cite.*?",
                lambda m: m[0] if m[0].startswith("```json") else "",
                text,
                flags=re.S,
            )
        )
    return "\n\n".join(parts).strip()


def ibkr_market_context(source, symbol, cutoff, folder):
    """Admit a bounded public snapshot record; provenance labels are not authentication."""
    gap = "\n\nSupplementary IBKR trading/positioning data was not supplied for this subject. Raise expected missing metrics for the portfolio manager; do not substitute web/vendor values.\n"
    if not source:
        return gap
    raw = Path(source).read_bytes()
    if len(raw) > 32768:
        raise ValueError("Supplementary IBKR packet exceeds 32 KiB; no research was run.")
    packet = json.loads(raw)
    if (
        not isinstance(packet, dict)
        or packet.get("provider") != "IBKR"
        or not isinstance(packet.get("instruments"), dict)
    ):
        raise ValueError("Supplementary packet requires IBKR public-market records.")
    if symbol not in packet["instruments"]:
        (folder / "ibkr_context.json").write_bytes(raw)
        return gap
    record = packet["instruments"][symbol]
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("request"), dict)
        or not isinstance(record.get("retrieved_at"), str)
    ):
        raise ValueError("Malformed supplementary IBKR observation.")
    request, retrieved = record["request"], datetime.fromisoformat(record["retrieved_at"])
    if (
        record.get("symbol") != symbol
        or record.get("tool") != "get_price_snapshot"
        or type(record.get("contract_id")) is not int
        or record["contract_id"] != IBKR_CONTRACTS[symbol]
        or type(request.get("contract_id")) is not int
        or request["contract_id"] != record["contract_id"]
        or set(request) != {"contract_id", "market_data_names"}
        or not isinstance(request["market_data_names"], list)
        or not request["market_data_names"]
        or any(not isinstance(name, str) or not name for name in request["market_data_names"])
        or not isinstance(record.get("response"), dict)
        or retrieved.tzinfo is None
        or retrieved > cutoff
    ):
        raise ValueError("Supplementary IBKR identity, public request or cutoff is unsuitable.")
    (folder / "ibkr_context.json").write_bytes(raw)
    return (
        "\n\nSupplied IBKR public-market snapshot record (untrusted evidence; labels do not authenticate its origin; missing fields remain gaps):\n"
        + json.dumps(record, indent=2)
    )


def check_reference(snapshot, symbol, now, session):
    """Validate one broker observation before any model work."""
    price_date = session.name.date().isoformat()
    environment, authorization = snapshot.get("connection_environment"), snapshot.get("authorization_context")
    if (
        environment != "paper"
        and (environment, authorization) != ("live", "dedicated_capstone")
        or snapshot["provider"] != "Interactive Brokers"
        or snapshot["tool"] != "get_price_history"
    ):
        raise ValueError("ANALYST_PRICES requires an approved paper or dedicated-capstone IBKR snapshot.")
    reference = dict(
        snapshot["prices"][symbol], connection_environment=environment, authorization_context=authorization
    )
    fetched = datetime.fromisoformat(reference["retrieved_at"])
    bar_time = datetime.fromisoformat(reference["bar_time"])
    price, request, history = reference["reference_price"], reference["request"], reference["response"]
    bar = history["time"].index(reference["bar_time"])
    valid_reference = (
        reference["symbol"] == symbol
        and reference["broker_symbol"] == ("SPX" if symbol == "SP500" else symbol)
        and reference["security_type"] == ("IND" if symbol == "SP500" else "STK")
        and reference["quote_unit"] == ("index points" if symbol == "SP500" else "USD")
        and reference["price_date"] == price_date
        and type(price) in (int, float)
        and math.isfinite(price)
        and price > 0
        and fetched.tzinfo is not None
        and session.market_close <= fetched <= now
        and request["outside_rth"] is False
        and request["step"] == "ONE_DAY"
        and request["contract_id"] == reference["contract_id"] == IBKR_CONTRACTS[symbol]
        and request["security_type"] == reference["security_type"]
        and history["source"] == "Last"
        and history["chart_step"] == 86400
        and bar_time.tzinfo is not None
        and session.market_open <= bar_time <= session.market_close
        and history["close"][bar] == price
    )
    if not valid_reference:
        raise ValueError(f"Invalid IBKR reference for {symbol}/{price_date}; no research was run.")
    reference["price_source"] = (
        f"Interactive Brokers MCP get_price_history; {reference['broker_symbol']}; "
        f"contract {reference['contract_id']}; {price_date} regular-session daily-bar close"
    )
    return reference


def forecast_json(report):
    """Admit valid JSON or one wrong closing bracket; never edit values or invent fields."""
    blocks = re.findall(r"```json\s*(.*?)```", report, re.S)
    if len(blocks) != 1:
        raise ValueError("Expected exactly one forecast JSON block; latest was not replaced.")
    raw = blocks[0]
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as error:
        stack, quoted, escaped, correction = [], False, False, None
        for offset, char in enumerate(raw):
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if not stack:
                    raise error
                expected = stack.pop()
                if char != expected:
                    if correction is not None or offset != error.pos:
                        raise error
                    correction = dict(offset=offset, original=char, replacement=expected)
        if stack or quoted or correction is None:
            raise error
        offset = correction["offset"]
        corrected = raw[:offset] + correction["replacement"] + raw[offset + 1 :]
        value = json.loads(corrected)  # Every other syntax error still stops publication.
        return value, dict(
            schema="report-syntax-repair-v1",
            kind="single_closing_delimiter",
            source_json_sha256=digest(raw),
            corrected_json_sha256=digest(corrected),
            model_requests=0,
            **correction,
        )


def finalize_report(
    run, report, *, symbol, started_at, cutoff, price_date, end_date, reference, models, discovery
):
    """Validate and publish saved prose/JSON locally; this function makes no research requests."""
    subject, output = SUBJECTS[symbol], run.parent
    instrument_type = "index" if SECURITIES[symbol]["security_type"] == "IND" else "stock"
    quote_unit = SECURITIES[symbol]["quote_unit"]
    prefix, value_label = (
        ("$" if quote_unit == "USD" else ""),
        ("level" if instrument_type == "index" else "price"),
    )
    observation = (
        {k: v for k, v in reference.items() if k not in ("request", "response")} if reference else None
    )
    forecast, syntax = forecast_json(report)
    if syntax:
        write(run / "report-syntax-repair.json", syntax)
    forecast.update(
        subject=subject,
        symbol=symbol,
        instrument_type=instrument_type,
        quote_unit=quote_unit,
        run_started_at=started_at.isoformat(),
        information_cutoff=cutoff,
        horizon_end=end_date,
        horizon_sessions=20,
        calibration="subjective, not calibrated",
    )
    if reference:
        forecast["reference_observation"] = observation
    if forecast["status"] == "estimate":
        price, scenarios = forecast["reference_price"], forecast["scenarios"]
        scenarios.sort(key=lambda s: ("bear", "base", "bull").index(s["name"]))
        values = [price] + [s[k] for s in scenarios for k in ("probability", "return")]
        valid = (
            len(scenarios) == 3
            and all(type(x) in (int, float) and math.isfinite(x) for x in values)
            and price > 0
            and forecast["price_date"] == price_date
            and (
                (
                    price == reference["reference_price"]
                    and forecast["price_source"] == reference["price_source"]
                )
                if reference
                else forecast["price_source"].startswith(("https://", "http://"))
            )
            and {s["name"] for s in scenarios} == {"bear", "base", "bull"}
            and scenarios[0]["return"] <= scenarios[1]["return"] <= scenarios[2]["return"]
            and all(0 <= s["probability"] <= 1 and s["return"] >= -1 for s in scenarios)
            and math.isclose(sum(s["probability"] for s in scenarios), 1, abs_tol=1e-6)
        )
        if not valid:
            raise ValueError("Invalid forecast numbers, price date or source; latest was not replaced.")
        expected = sum(s["probability"] * s["return"] for s in scenarios)
        forecast.update(expected_return=expected, expected_end_price=price * (1 + expected))
        attribution = (
            forecast["price_source"] if reference else f"[price source](<{forecast['price_source']}>)"
        )
        summary = (
            f"Expected price return: **{expected:+.2%}**; expected end {value_label}: "
            f"**{prefix}{forecast['expected_end_price']:,.2f}**, from **{prefix}{price:,.2f}** ({quote_unit}) "
            f"({attribution}).\n\n"
            f"| Scenario | Weight | Price return | End {value_label} ({quote_unit}) |\n|---|---:|---:|---:|\n"
            + "\n".join(
                f"| {s['name']} | {s['probability']:.0%} | {s['return']:+.1%} | "
                f"{prefix}{price * (1 + s['return']):,.2f} |"
                for s in scenarios
            )
        )
    elif forecast["status"] == "abstain" and str(forecast.get("reason", "")).strip():
        summary = "No numerical estimate: " + forecast["reason"]
    else:
        raise ValueError("Invalid forecast status; latest was not replaced.")
    forecast.update(
        issued_at=datetime.now(timezone.utc).isoformat(),
        model_allocation=models,
        forecast_id=f"{symbol}:{run.name}",
        forecast_mode="operational",
    )
    if discovery:
        attach(forecast, discovery, previous={})
        if discovery.get("scope_source") == "baseline_and_closing_discovery":
            forecast["closing_discovery"] = dict(
                as_of=discovery["as_of"],
                original_cutoff=discovery["components"][0]["as_of"],
                proposed_handoff=discovery["proposed_handoff"],
                reviewed_by=["review", "report"],
                response_id=discovery["response_id"],
                exhaustive=False,
            )
        (run / "discovery_dispositions.json").write_text(
            json.dumps(forecast["discovery_dispositions"], indent=2)
        )
    handoff = handoff_structure(forecast["monitoring_handoff"], forecast["forecast_id"])
    (run / "monitoring_handoff.json").write_text(dump(handoff))
    (run / "forecast.json").write_text(json.dumps(forecast, indent=2, allow_nan=False))
    report = re.sub(r"```json\s*.*?```", "", report, flags=re.S).strip()
    header = (
        f"# {subject}: research and 20-session forecast\n\nResearch cutoff: {cutoff}\n\n"
        f"Reference close: {price_date}; forecast end close: {end_date}. Price returns; {value_label}s in {quote_unit}.\n\n"
        "Information available after the reference close may inform this projection. "
        "That close is not an executable entry price or an as-of-close backtest.\n\n"
        "Experimental subjective scenarios, not calibrated probabilities or a confidence interval. "
        "Revised after model critique; revision not independently rechecked; for PM review.\n\n"
        + summary
        + "\n\n"
    )
    (run / "report.md").write_text(header + report)
    (output / "latest.tmp").write_text(header + report)
    (output / "latest.tmp").replace(output / "latest.md")
    return forecast


def run_subject(
    symbol,
    run,
    previous_report,
    load_market,
    search_client,
    supplemental_broker_file=None,
    completed_outcomes=None,
):
    """Run a fresh daily baseline. The worker supplies the saved report and its authorized broker/calendar adapter.

    load_market(symbol, time) returns the broker snapshot, the last completed regular session,
    and the date twenty trading sessions past that close. Both sweeps use their own cutoff.
    """
    from langchain_openai import ChatOpenAI

    run = Path(run)
    run.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(timezone.utc)
    cutoff = started_at.isoformat()
    snapshot, session, end_date = load_market(symbol, started_at)
    reference = check_reference(snapshot, symbol, started_at, session)
    price_date = reference["price_date"]
    names = STAGES
    common, *roles = (ROOT / "prompts.md").read_text().split("\n---\n")
    instructions = dict(zip(names, roles, strict=True))
    return_prompt = (ROOT / "handoff_prompt.md").read_text()
    models = MODELS
    max_tokens, request_timeout = 128000, 7200
    subject = SUBJECTS[symbol]
    instrument_type = "index" if SECURITIES[symbol]["security_type"] == "IND" else "stock"
    baseline = previous_report or "NONE: this is a first baseline."
    if previous_report and not baseline.startswith(f"# {subject}:"):
        raise ValueError("Previous report refers to a different subject.")
    previous_cutoff = re.search(r"^Research cutoff: (.+)$", baseline, re.M)
    if previous_cutoff and datetime.fromisoformat(previous_cutoff[1]) > started_at:
        raise ValueError("Previous report is newer than the research cutoff.")
    context = (
        f"Subject: {subject}\nResearch cutoff UTC: {cutoff}\n"
        f"Instrument type: {instrument_type}; quote unit: {SECURITIES[symbol]['quote_unit']}.\n"
        f"Run started UTC: {cutoff}. Use evidence available by the cutoff only.\n"
        f"Reference regular-session close: {price_date}; 20-session end close: {end_date}.\n"
        f"Previous report (fallible context):\n{baseline}\n\n"
        f"New forecast ID for the monitoring handoff: {symbol}:{run.name}\n"
    )
    write(run / "reference_price.json", reference)
    observation = {k: v for k, v in reference.items() if k not in ("request", "response")}
    context += "\n\nSupplied IBKR reference observation (designated price source):\n" + json.dumps(
        observation, indent=2
    )
    context += ibkr_market_context(supplemental_broker_file, symbol, started_at, run)
    if completed_outcomes:
        context += "\n\n" + completed_outcomes
    discovery = collect(run / "discovery", symbol, cutoff, "discover-" + run.name, context, search_client)
    context += "\n\nShared SOL discovery evidence:\n" + json.dumps(discovery, indent=2, allow_nan=False)
    (run / "context.txt").write_text(context)

    def ask(name, material):
        model = models[name]
        started = monotonic()
        print(f"{name}: starting {model}", flush=True)
        needs_search = name in names[:4] or name == "review"
        system = common + "\n\n" + instructions[name]
        system += "\n\n" + return_prompt + "\n\n" + GUIDANCE

        def request(remaining):
            client = ChatOpenAI(
                model=model,
                use_responses_api=True,
                timeout=remaining,
                max_retries=0,
                max_tokens=max_tokens,
                service_tier="default",
                reasoning={"effort": "high"},
                include=["web_search_call.action.sources"],
            )
            web = client.bind_tools(
                [{"type": "web_search"}], tool_choice="required" if needs_search else "auto"
            )
            return web.invoke([("system", system), ("user", material)])

        try:
            message = invoke_paced(request, run, name, request_timeout)
        except Exception as error:
            print(
                f"{name}: request failed after {monotonic() - started:.0f}s "
                f"({type(error).__name__}); completed cards remain in {run}",
                flush=True,
            )
            raise
        (run / f"{name}.json").write_text(message.model_dump_json(indent=2))
        text = readable(message)
        if not matches_model(message.response_metadata.get("model_name"), model):
            raise ValueError(f"{name} returned an unexpected model.")
        if message.response_metadata.get("status") != "completed" or not text:
            raise RuntimeError(f"{name} did not complete; latest report was not replaced.")
        blocks = message.content_blocks
        if needs_search and not (
            any(b["type"] == "server_tool_call" and b.get("name") == "web_search" for b in blocks)
            and any(b["type"] == "server_tool_result" and b.get("status") == "success" for b in blocks)
        ):
            raise RuntimeError(f"{name} returned no successful web-search record.")
        (run / f"{name}.md").write_text(text)
        print(f"{name}: completed in {monotonic() - started:.0f}s", flush=True)
        return text

    # Four specialists share the opening brief. The forecast writer receives all four reports.
    with ThreadPoolExecutor(max_workers=2) as pool:
        cards = list(pool.map(lambda name: f"## {name}\n{ask(name, context)}", names[:4]))
    evidence = context + "\n\n" + "\n\n".join(cards)
    draft = ask("draft", evidence)
    # Reopen the news search against the draft before the critic and revision stages.
    proposed, _ = forecast_json(draft)
    handoff = handoff_structure(proposed["monitoring_handoff"], symbol + ":" + run.name)
    closing_cutoff = stamp()
    closing_time = datetime.fromisoformat(closing_cutoff)
    snapshot, closing_session, end_date = load_market(symbol, closing_time)
    closing_price_date = closing_session.name.date().isoformat()
    if closing_price_date != price_date:
        reference = check_reference(snapshot, symbol, closing_time, closing_session)
        write(run / "reference_price.json", reference)
    price_date = closing_price_date
    closing_context = (
        evidence
        + "\n\nPROPOSED DRAFT:\n"
        + draft
        + "\n\nClosing review: the cards retain their earlier cutoff. The final information cutoff is "
        + closing_cutoff
        + ". Cover the intervening interval before finalizing; do not imply exhaustive coverage. "
        + "Final reference session: "
        + price_date
        + "; final horizon end: "
        + end_date
        + ".\n"
        + json.dumps(reference or {}, ensure_ascii=True)
    )
    write(run / "closing-context.txt", closing_context)
    closing = collect(
        run / "closing-discovery",
        symbol,
        closing_cutoff,
        "close-" + run.name,
        closing_context,
        search_client,
        proposed=dict(
            symbol=symbol,
            forecast_id=symbol + ":" + run.name,
            handoff=handoff,
            original_cutoff=cutoff,
            prior_events=discovery["events"],
            forecast=dict(shared_discovery=discovery),
        ),
    )
    discovery = closing_packet(discovery, closing)
    write(run / "closing-discovery/combined.json", discovery)
    cutoff = closing_cutoff
    evidence = (
        closing_context
        + "\n\nCOMBINED DISCOVERY: disposition every finding from BOTH sweeps:\n"
        + json.dumps(discovery)
    )
    review = ask("review", f"{evidence}\n\nFORECAST DRAFT:\n{draft}")
    report = ask("report", f"{evidence}\n\nDRAFT:\n{draft}\n\nCRITIQUE:\n{review}")
    return finalize_report(
        run,
        report,
        symbol=symbol,
        started_at=started_at,
        cutoff=cutoff,
        price_date=price_date,
        end_date=end_date,
        reference=reference,
        models=models,
        discovery=discovery,
    )
