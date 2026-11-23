"""Date each evidence item and flag its age at the time the portfolio was reviewed."""

import re
from datetime import date, timedelta

from ledger import EASTERN, instant


def dated(value, parse):
    try:
        return parse(value) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def source_dates(inputs):
    import pandas_market_calendars as calendars

    at = instant(inputs["as_of"])
    market = inputs["market"]
    session = date.fromisoformat(
        inputs.get("intended_session")
        or market.get("session_date")
        or at.astimezone(EASTERN).date().isoformat()
    )
    exchange = calendars.get_calendar("NYSE")
    days = exchange.valid_days(session - timedelta(days=16), session - timedelta(days=1))
    prior = days[-1].date()
    closing = market.get("price_mode") == "last_close_rehearsal"
    rows = []

    def add(group, name, stamp, received, horizon, status, reason):
        rows.append(
            {
                "group": group,
                "name": name,
                "observed": stamp,
                "received": received,
                "horizon": horizon,
                "status": status,
                "reason": reason,
            }
        )

    for sym, item in sorted(inputs["analysts"].items()):
        f = item["forecast"]
        stamp, issued, end = (f.get(k) for k in ("information_cutoff", "issued_at", "horizon_end"))
        cutoff, publication, horizon = (
            dated(stamp, instant),
            dated(issued, instant),
            dated(end, date.fromisoformat),
        )
        status, note = "Current", "Research for the designated session."
        if not cutoff or not publication:
            status, note = "Missing", "Cutoff or issue time unavailable."
        elif cutoff > at or publication > at:
            status, note = "Review", "Source is later than the review."
        elif end and not horizon:
            status, note = "Review", "Forecast horizon date is unreadable."
        elif horizon and horizon < session:
            status, note = "Expired", "Forecast horizon has ended."
        elif (
            cutoff.astimezone(EASTERN).date() < prior
            or date.fromisoformat(inputs["batch_date"]) < prior
        ):
            status, note = "Stale", "Older than the preceding NYSE session."
        add(
            "Analysts",
            sym,
            stamp if cutoff else None,
            issued if publication else None,
            end if horizon else None,
            status,
            note,
        )
        price_day = f.get("price_date")
        ref_day = dated(price_day, date.fromisoformat)
        ref_status = (
            "Missing"
            if not ref_day
            else "Stale"
            if ref_day < prior
            else "Review"
            if ref_day > session
            else "Reference"
        )
        add(
            "Analyst price references",
            sym,
            price_day if ref_day else None,
            None,
            None,
            ref_status,
            "Report reference only. The hedge uses its separate IBKR observation.",
        )

    g = inputs.get("gdelt") or {}
    window = (g.get("disclosure") or {}).get("observation_window") or {}
    end = window.get("end")
    limit = inputs["policy"].get("gdelt_review_hours", 24)
    news_end = dated(end, instant)
    hours = (at - news_end).total_seconds() / 3600 if news_end else None
    status = (
        "Missing"
        if hours is None
        else "Review"
        if hours < 0
        else "Stale"
        if hours > limit
        else "Current"
    )
    note = (
        f"News cohort ended {hours:.1f} hours before review. Warning threshold {limit} hours."
        if hours is not None
        else inputs.get("gdelt_gap") or "No dated observation window."
    )
    delivery = (
        g.get("delivery_created_at") if dated(g.get("delivery_created_at"), instant) else None
    )
    add("GDELT", "Current-news cohort", end if news_end else None, delivery, None, status, note)
    issued = (g.get("research") or {}).get("created_at")
    if g:
        memo_time = dated(issued, instant)
        add(
            "GDELT",
            "Research memo",
            issued if memo_time else None,
            delivery,
            None,
            "Missing"
            if not memo_time or not delivery
            else "Review"
            if memo_time > at or instant(delivery) > at
            else "Dated",
            "Memo completion and handoff publication. News freshness uses the underlying cohort.",
        )
    seconds = inputs["policy"].get("max_mark_age_seconds", 120)
    for sym, q in sorted(market["quotes"].items()):
        stamp = q.get("valuation_at" if closing else "observed_at")
        received = q.get("received_at")
        age = (at - instant(stamp)).total_seconds() if stamp else None
        if age is None or not received:
            status, note = "Missing", "Mark time or receipt time unavailable."
        elif age < 0 or instant(received) > at:
            status, note = "Review", "Observation or receipt postdates the review."
        elif closing:
            status, note = (
                ("Replay close", "Dated regular-hours close used for this rehearsal.")
                if q.get("session_date") == session.isoformat()
                else ("Stale", "Bar has a different session date.")
            )
        elif q.get("mode") != "REALTIME" or q.get("is_close") or q.get("halted"):
            status, note = "Review", "Delayed, closing, halted or unqualified feed."
        elif age > seconds:
            status, note = "Stale", f"Price was {age:.0f} seconds old. Limit {seconds} seconds."
        else:
            status, note = (
                "Current",
                f"Observed {age:.0f} seconds before review. Limit {seconds} seconds.",
            )
        add("IBKR prices", sym, stamp, received, None, status, note)
    policy = inputs["policy"]
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", policy.get("beta_source", ""))
    beta_end = policy.get("beta_as_of") or (dates[-1] if dates else None)
    beta_day = dated(beta_end, date.fromisoformat)
    status = (
        "Missing"
        if not beta_day
        else "Stale"
        if beta_day < prior
        else "Review"
        if beta_day > session
        else "Current"
    )
    add(
        "Portfolio",
        "Beta calibration",
        beta_end if beta_day else None,
        None,
        None,
        status,
        policy.get("beta_source") or "No calibration date supplied.",
    )
    marks = [p.get("marked_at") for p in inputs["portfolio"].get("equities", {}).values()]
    oldest = min(marks, key=instant) if marks and all(marks) else None
    if not oldest:
        status = "Missing"
    elif instant(oldest) > at:
        status = "Review"
    elif instant(oldest).astimezone(EASTERN).date() < session or (
        not closing and (at - instant(oldest)).total_seconds() > seconds
    ):
        status = "Stale"
    else:
        status = "Current"
    add(
        "Portfolio",
        "Oldest equity mark",
        oldest,
        None,
        None,
        status,
        "Oldest mark among the six held equities.",
    )
    upcoming = exchange.schedule(session + timedelta(days=1), session + timedelta(days=14))
    return {
        "as_of": at.isoformat(),
        "session": session.isoformat(),
        "rows": rows,
        "next_open": upcoming.iloc[0]["market_open"].isoformat(),
        "max_quote_age_seconds": seconds,
    }
