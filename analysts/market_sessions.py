"""Exchange calendars for the configured US quote feeds; no broker/model calls."""

from datetime import timedelta

import pandas_market_calendars as mcal

# Listing calendars, not a weekday approximation. WMT transferred in December 2025.
from securities import CALENDARS

# Add a symbol only after its account entitlement, MCP availability and fees are
# checked. Exchange opening hours alone are not evidence of data access.
EXTENDED_HOURS_VERIFIED = frozenset()


def schedule(symbol, now, days_ahead=14, extended=False):
    if now.tzinfo is None:
        raise ValueError("A timezone-aware market clock is required.")
    options = dict(start="pre", end="post") if extended and symbol != "SP500" else {}
    return mcal.get_calendar(CALENDARS[symbol]).schedule(
        start_date=(now - timedelta(days=14)).date(),
        end_date=(now + timedelta(days=days_ahead)).date(),
        **options,
    )


def completed_session(symbol, now):
    sessions = schedule(symbol, now)
    return sessions[sessions.market_close <= now].iloc[-1]


def quote_session(symbol, now):
    """Calendar availability and verified feed access are distinct gates."""
    sessions = schedule(symbol, now, extended=True)
    opening, closing = ("market_open", "market_close") if symbol == "SP500" else ("pre", "post")
    active = sessions[(sessions[opening] <= now) & (now < sessions[closing])]
    state = "closed"
    if not active.empty:
        row = active.iloc[0]
        state = (
            "pre_market"
            if now < row.market_open
            else "post_market"
            if now >= row.market_close
            else "regular"
        )
    previous = sessions[sessions.market_close <= now].iloc[-1]
    extended = symbol in EXTENDED_HOURS_VERIFIED and symbol != "SP500"
    enabled_opening = opening if extended else "market_open"
    fetch = state == "regular" or (extended and state in ("pre_market", "post_market"))
    return dict(
        calendar=CALENDARS[symbol],
        state=state,
        fetch_quote=fetch,
        skip_reason=None
        if fetch
        else ("market_closed" if state == "closed" else "entitlement_unverified"),
        checked_at=now.isoformat(),
        last_regular_session=previous.name.date().isoformat(),
        last_regular_close_at=previous.market_close.isoformat(),
        next_quote_session_at=sessions.loc[sessions[enabled_opening] > now, enabled_opening]
        .iloc[0]
        .isoformat(),
        feed_scope="cash_index_regular"
        if symbol == "SP500"
        else ("equity_regular_and_extended" if extended else "equity_regular_extended_unverified"),
        overnight_feed_enabled=False,
    )
