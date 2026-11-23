"""Configured forecast/calendar mapping and read-only IBKR input diagnostics; no API calls."""

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from price_watch import DEFAULT_POLICY, instant, positive
from securities import SECURITIES, CONTRACTS, CALENDARS

CALENDAR = mcal.get_calendar("NYSE")
EASTERN = ZoneInfo("America/New_York")


# Validate a forecast's 20-session horizon and map its price fields into a monitor watch.
def forecast_fields(forecast, *, price_basis="raw"):
    """Map a native DE estimate; price_basis is caller-declared, not provider-qualified."""
    f = forecast
    if (
        f.get("status") != "estimate"
        or f.get("symbol") not in SECURITIES
        or f.get("instrument_type")
        != ("index" if SECURITIES[f["symbol"]]["security_type"] == "IND" else "stock")
        or f.get("quote_unit") != SECURITIES[f["symbol"]]["quote_unit"]
        or f.get("horizon_sessions") != 20
    ):
        raise ValueError(
            "Require a configured instrument estimate with matching units for 20 sessions."
        )
    if not isinstance(price_basis, str) or not price_basis.strip():
        raise ValueError("An explicit price basis is required.")
    start, end = map(date.fromisoformat, (f["price_date"], f["horizon_end"]))
    schedule = mcal.get_calendar(CALENDARS[f["symbol"]]).schedule(start, end)
    if len(schedule) != 21 or schedule.index[0].date() != start or schedule.index[-1].date() != end:
        raise ValueError(
            "Forecast dates must bind exactly 20 NYSE sessions after the reference close."
        )
    reference, expiry = schedule.market_close.iloc[0], schedule.market_close.iloc[-1]
    issued = instant(f["issued_at"])
    if not reference <= issued < expiry:
        raise ValueError("Forecast issuance must follow its reference close and precede expiry.")
    return dict(
        id=f["forecast_id"],
        symbol=f["symbol"],
        currency=f["quote_unit"],
        price_basis=price_basis,
        reference_at=reference.isoformat(),
        issued_at=f["issued_at"],
        valid_until=expiry.isoformat(),
        reference_price=f["reference_price"],
        target_price=f["expected_end_price"],
        horizon_sessions=20,
    )


# Measure elapsed trading sessions between the reference close and the observation.
def session_fields(reference_at, observed_at, *, symbol="DE"):
    """Use actual regular-session lengths; CLOSED means outside the regular session."""
    reference, observed = map(instant, (reference_at, observed_at))
    if observed < reference:
        raise ValueError("Observation precedes the frozen reference.")
    schedule = mcal.get_calendar(CALENDARS[symbol]).schedule(
        reference.astimezone(EASTERN).date(), observed.astimezone(EASTERN).date()
    )
    if schedule.empty or schedule.market_close.iloc[0] != reference:
        raise ValueError("Reference must be an actual NYSE session close.")
    elapsed = sum(
        max(0.0, min(1.0, (observed - row.market_open) / (row.market_close - row.market_open)))
        for row in schedule.iloc[1:].itertuples()
    )
    regular = any(row.market_open <= observed <= row.market_close for row in schedule.itertuples())
    return dict(
        market_session="REGULAR" if regular else "CLOSED",
        elapsed_sessions=float(elapsed),
        at_session_open=any(observed == row.market_open for row in schedule.itertuples()),
    )


# Check whether a saved broker bar series provides the evidence required by the monitor.
def qualify_history(raw, received_at, *, checked_at=None, request=None):
    """Describe saved minute bars without inventing the metadata needed for live use."""
    received = instant(received_at)
    checked = instant(checked_at) if checked_at else datetime.now(timezone.utc)
    if received > checked:
        raise ValueError("Receipt must not follow the qualification check.")
    history = raw.get("structuredContent", {})
    if not isinstance(history, dict):
        history = {}
    checks = {}

    # Record the result and explanation for one bar-history check.
    def check(name, passed, detail):
        checks[name] = dict(status="pass" if passed else "fail", detail=detail)

    check(
        "mcp_response",
        not raw.get("isError") and bool(history),
        "Require a successful MCP structuredContent object.",
    )
    expected = dict(
        contract_id=6393,
        security_type="STK",
        step="ONE_MIN",
        outside_rth=False,
        include_corporate_actions=True,
    )
    check(
        "request",
        bool(request)
        and all(request.get(k) == v for k, v in expected.items())
        and (("period" in request) != ("step_count" in request)),
        "Require the saved DE regular-session minute-bar request with corporate actions requested.",
    )
    if request is None:
        checks["request"]["status"] = "missing"
    check(
        "bar_kind",
        history.get("source") == "Last" and history.get("chart_step") == 60,
        "Require Last one-minute bars; this does not establish completion or realtime availability.",
    )
    try:
        times = [instant(t) for t in history["time"]]
        closes = history["close"]
        arrays_ok = (
            bool(times)
            and times == sorted(set(times))
            and len(closes) == len(times)
            and all(positive(v) for v in closes)
        )
    except (KeyError, TypeError, ValueError):
        times, arrays_ok = [], False
    check(
        "bar_arrays", arrays_ok, "Require ordered unique zoned labels and matching positive closes."
    )
    check(
        "label_chronology",
        bool(times) and max(times) <= received,
        "A returned bar label cannot follow receipt; label time is not established observation time.",
    )
    regular = False
    if arrays_ok:
        schedule = CALENDAR.schedule(
            times[0].astimezone(EASTERN).date(), times[-1].astimezone(EASTERN).date()
        )
        regular = all(
            any(row.market_open <= t <= row.market_close for row in schedule.itertuples())
            for t in times
        )
    check("session_labels", regular, "Require labels inside actual NYSE regular sessions.")
    missing = {
        "bar_label_semantics": "Document whether labels identify the start or end of each bar.",
        "bar_completion": "Establish completion and price-observation time for the chosen bar.",
        "availability": "Document the delay field and establish live availability; omission is unknown.",
        "price_basis": "Establish a price basis comparable with the frozen reference and history.",
        "corporate_action_coverage": "Establish complete relevant action coverage and its price treatment.",
        "halt_status": "Establish suitable halt/status coverage for the observation.",
    }
    checks.update(
        {name: dict(status="unverified", detail=detail) for name, detail in missing.items()}
    )
    return dict(
        schema="monitor-input-qualification-v1",
        qualified=False,
        note="Diagnostic only; recent bar labels do not qualify a live price observation.",
        received_at=received.isoformat(),
        checked_at=checked.isoformat(),
        checks=checks,
        latest_bar_label=max(times).isoformat() if times else None,
        label_age_seconds=(checked - max(times)).total_seconds() if times else None,
        reported_delay=history.get("delayed"),
        reported_corporate_actions=history.get("corp_actions"),
        missing_facts=list(missing),
    )


# Inspect a saved quote's prices, timestamps, and suitability for monitoring.
def qualify_snapshot(raw, received_at, *, checked_at=None, request=None, symbol="DE"):
    """Inspect a saved snapshot; timestamp interpretation is not provider qualification."""
    received = instant(received_at)
    checked = instant(checked_at) if checked_at else datetime.now(timezone.utc)
    if received > checked:
        raise ValueError("Receipt must not follow the qualification check.")
    try:
        payloads = [raw["structuredContent"]] if raw.get("structuredContent") is not None else []
        payloads += [
            json.loads(c["text"]) for c in raw.get("content", []) if c.get("type") == "text"
        ]
        data = payloads[0]
        consistent = isinstance(data, dict) and bool(data) and all(p == data for p in payloads)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        data, consistent = {}, False
    if not isinstance(data, dict):
        data = {}
    last, top, quote = [
        data.get(k) if isinstance(data.get(k), dict) else {}
        for k in ("last", "top-status", "bid-ask")
    ]
    try:
        observed = (
            datetime.fromtimestamp(last["ts"], timezone.utc) if positive(last.get("ts")) else None
        )
    except (ValueError, OverflowError, OSError):
        observed = None
    receipt_age = (received - observed).total_seconds() if observed else None
    check_age = (checked - observed).total_seconds() if observed else None
    checks = {}

    # Record the result and explanation for one quote-snapshot check.
    def check(name, passed, detail):
        checks[name] = dict(status="pass" if passed else "fail", detail=detail)

    check(
        "mcp_response",
        isinstance(raw, dict) and not raw.get("isError") and consistent,
        "Require successful, consistent JSON object payloads; text and structured forms must agree.",
    )
    check(
        "request",
        isinstance(request, dict)
        and request.get("contract_id") == CONTRACTS[symbol]
        and isinstance(request.get("market_data_names"), list)
        and all(name in request["market_data_names"] for name in ("last", "top_status")),
        "Require the saved instrument request for last and top_status; response identity is not inferred.",
    )
    if request is None:
        checks["request"]["status"] = "missing"
    check("last_price", positive(last.get("price")), "Require a finite positive last price.")
    check(
        "reported_realtime",
        top.get("status") == "REALTIME",
        "Require explicit reported REALTIME status.",
    )
    check("reported_not_halted", last.get("halted") is False, "Require explicit halted=false.")
    check("reported_not_close", last.get("is_close") is False, "Require explicit is_close=false.")
    check(
        "timestamp_chronology",
        observed is not None and observed <= received,
        "Under the Unix-seconds interpretation, last.ts must be numeric, nonboolean and no later than receipt.",
    )
    check(
        "freshness",
        check_age is not None and 0 <= check_age <= DEFAULT_POLICY["max_age_seconds"],
        "Apply the existing freshness limit at check time under the stated timestamp interpretation.",
    )
    missing = {
        "timestamp_semantics": "Confirm last.ts units and observation meaning; Unix seconds is an explicit diagnostic interpretation.",
        "price_basis": "Establish comparability with the frozen reference and calibration history.",
        "corporate_action_coverage": "Establish complete relevant action coverage and its price treatment.",
        "bid_ask_observation_time": "No bid/ask time mapping is established; receipt time and last.ts are not substitutes.",
    }
    checks.update(
        {name: dict(status="unverified", detail=detail) for name, detail in missing.items()}
    )
    return dict(
        schema="monitor-snapshot-qualification-v1",
        qualified=False,
        note="Read-only diagnostic; no live price observation is admitted.",
        received_at=received.isoformat(),
        checked_at=checked.isoformat(),
        reported=data,
        timestamp_interpretation="last.ts interpreted as Unix seconds; units and meaning unverified",
        interpreted_last_at=observed.isoformat() if observed else None,
        age_at_receipt_seconds=receipt_age,
        age_at_check_seconds=check_age,
        max_age_seconds=DEFAULT_POLICY["max_age_seconds"],
        reported_bid_ask_ts=quote.get("ts"),
        checks=checks,
        missing_facts=list(missing),
    )


# Read the selected input and report its broker-data checks.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("response", type=Path, help="Saved MCP market-data response JSON.")
    parser.add_argument("--kind", choices=["history", "snapshot"], default="history")
    parser.add_argument(
        "--request", type=Path, help="Saved exact request JSON; absence remains explicit."
    )
    parser.add_argument(
        "--received-at", help="Zoned receipt time; defaults to the retrieval sidecar."
    )
    parser.add_argument("--checked-at", help="Zoned diagnostic time; defaults to current UTC.")
    args = parser.parse_args()
    received = (
        args.received_at or args.response.with_suffix(".retrieved_at.txt").read_text().strip()
    )
    qualify = qualify_history if args.kind == "history" else qualify_snapshot
    result = qualify(
        json.loads(args.response.read_text()),
        received,
        checked_at=args.checked_at,
        request=json.loads(args.request.read_text()) if args.request else None,
    )
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
