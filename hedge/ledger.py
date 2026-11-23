"""Local paper portfolio. Run with --help. Uses only the Python standard library."""

import argparse
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo


MES = re.compile(r"MES-[0-9]{4}(03|06|09|12)")
MULTIPLIER = 5
CENT = Decimal(".01")
EASTERN = ZoneInfo("America/New_York")


def instant(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Include the UTC offset in each timestamp.")
    return stamp.astimezone(timezone.utc)


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Expected a finite price or amount.")
    return result


def usd(amount):
    return format(amount.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def open_book(db, seed=None):
    db.execute("""CREATE TABLE IF NOT EXISTS journal (
        id TEXT PRIMARY KEY, at TEXT NOT NULL, decision_day TEXT UNIQUE,
        request TEXT NOT NULL, receipt TEXT NOT NULL)""")
    row = db.execute("SELECT request FROM journal WHERE id = 'opening'").fetchone()
    if row:
        return json.loads(row[0])
    if seed is None:
        seed = json.loads(Path(__file__).with_name("opening.json").read_text())
    db.execute(
        "INSERT INTO journal VALUES ('opening', ?, NULL, ?, ?)",
        (instant(seed["at"]).isoformat(), json.dumps(seed), '{"status":"opening"}'),
    )
    return seed


def outcome(event, seed):
    kind = event["kind"]
    if kind == "mark":
        prices = event["prices"]
        if not isinstance(prices, dict) or not prices:
            raise ValueError("Supply at least one instrument price.")
        for symbol, value in prices.items():
            if symbol not in seed["equities"] and not MES.fullmatch(symbol):
                raise ValueError(f"Unknown instrument: {symbol}")
            if number(value) <= 0:
                raise ValueError(f"Expected a positive mark for {symbol}.")
        return {"status": "marked"}
    if kind == "income":
        amount = number(event["amount_usd"])
        if amount <= 0 or amount != amount.quantize(CENT):
            raise ValueError("Supply positive dividends or interest in dollars, to the cent.")
        return {"status": "income"}
    if kind in ("hold", "defer"):
        reason = event.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("State the reason for holding or deferring.")
        return {"status": kind, "reason": reason}
    if kind != "adjust":
        raise ValueError("Use mark, income, adjust, hold or defer.")
    qty = event["contracts"]
    if not MES.fullmatch(event["contract"]) or type(qty) is not int or not qty:
        raise ValueError(
            "Supply an exact quarterly MES-YYYYMM contract and signed nonzero contract count."
        )
    fee = number(event["fee_usd"])
    if fee < 0 or fee != fee.quantize(CENT):
        raise ValueError("Supply the total nonnegative fee in dollars, to the cent.")
    quote = event.get("quote") or {}
    if quote.get("qualified") is not True:
        return {"status": "defer", "reason": "No qualified MES quote."}
    if quote.get("contract") != event["contract"]:
        return {"status": "defer", "reason": "Quote is for a different contract."}
    if any(quote.get(k) is None for k in ("observed_at", "valid_until", "bid", "ask")):
        return {"status": "defer", "reason": "Incomplete MES quote."}
    if not instant(quote["observed_at"]) <= instant(event["at"]) <= instant(quote["valid_until"]):
        return {"status": "defer", "reason": "MES quote is outside its validity window."}
    bid, ask = number(quote["bid"]), number(quote["ask"])
    if bid <= 0 or ask < bid or bid % Decimal(".25") or ask % Decimal(".25"):
        return {"status": "defer", "reason": "Invalid MES bid or ask."}
    return {
        "status": "filled",
        "side": "buy" if qty > 0 else "sell",
        "contracts": abs(qty),
        "fill_price": str(ask if qty > 0 else bid),
    }


def record(db, seed, event):
    key = event["id"]
    if key == "opening" or not all(
        isinstance(v, str) and v.strip() for v in (key, event.get("source"))
    ):
        raise ValueError("Supply a unique event ID and a named source.")
    prior = db.execute("SELECT request, receipt FROM journal WHERE id = ?", (key,)).fetchone()
    request = json.dumps(event, sort_keys=True, allow_nan=False)
    if prior:
        if request != prior[0]:
            raise ValueError(f"Event ID {key} already belongs to different data.")
        return json.loads(prior[1])
    when = instant(event["at"])
    if when < instant(seed["at"]):
        raise ValueError("Observation predates the opening portfolio.")
    result = outcome(event, seed)
    decision = event["kind"] in ("adjust", "hold", "defer")
    day = when.astimezone(EASTERN).date().isoformat() if decision else None
    if day and db.execute("SELECT 1 FROM journal WHERE decision_day = ?", (day,)).fetchone():
        raise ValueError(f"A portfolio decision is already recorded for {day} Eastern.")
    db.execute(
        "INSERT INTO journal VALUES (?, ?, ?, ?, ?)",
        (key, when.isoformat(), day, request, json.dumps(result)),
    )
    return result


def entries(db, cutoff):
    rows = db.execute(
        """SELECT request, receipt FROM journal WHERE id != 'opening' AND at <= ?
                         ORDER BY at, rowid""",
        (cutoff,),
    )
    return [dict(json.loads(request), receipt=json.loads(receipt)) for request, receipt in rows]


def statement(seed, events):
    stocks = seed["equities"]
    base = number(seed["cash_usd"])
    opening = base + sum(number(v["value_usd"]) for v in stocks.values())
    marks = {
        s: (number(v["value_usd"]) / v["shares"], seed["at"], seed["source"])
        for s, v in stocks.items()
    }
    lots = defaultdict(deque)
    realized = fees = income = Decimal(0)
    for e in events:
        receipt = e["receipt"]
        if receipt["status"] == "income":
            income += number(e["amount_usd"])
        if receipt["status"] == "marked":
            for s, value in e["prices"].items():
                marks[s] = number(value), e["at"], e["source"]
        if receipt["status"] != "filled":
            continue
        symbol, qty, price = e["contract"], e["contracts"], number(receipt["fill_price"])
        fees += number(e["fee_usd"])
        book = lots[symbol]
        while book and qty * book[0][0] < 0:
            held, entry = book[0]
            closed = min(abs(held), abs(qty)) * (1 if held > 0 else -1)
            realized += closed * (price - entry) * MULTIPLIER
            qty += closed
            book[0][0] -= closed
            if not book[0][0]:
                book.popleft()
        if qty:
            book.append([qty, price])
        observed = e["quote"]["observed_at"]
        if symbol not in marks or instant(observed) >= instant(marks[symbol][1]):
            marks[symbol] = price, observed, "Simulated fill from " + e["source"]
    equity, futures = {}, {}
    equity_value = unrealized = Decimal(0)
    for s, stock in stocks.items():
        price, at, source = marks[s]
        value = (stock["shares"] * price).quantize(CENT, rounding=ROUND_HALF_UP)
        equity_value += value
        equity[s] = dict(
            shares=stock["shares"],
            mark=str(price),
            marked_at=at,
            source=source,
            value_usd=usd(value),
        )
    for s, book in lots.items():
        price, at, source = marks[s]
        pnl = sum((q * (price - entry) * MULTIPLIER for q, entry in book), Decimal(0))
        unrealized += pnl
        futures[s] = dict(
            contracts=sum(q for q, _ in book),
            mark=str(price),
            marked_at=at,
            source=source,
            unrealized_usd=usd(pnl),
        )
    cash = base + income + realized - fees
    nav = cash + equity_value + unrealized
    return dict(
        as_of=events[-1]["at"] if events else seed["at"],
        opening_assumption=seed["opening_futures_assumption"],
        equities=equity,
        futures=futures,
        equities_usd=usd(equity_value),
        cash_usd=usd(cash),
        realized_futures_usd=usd(realized),
        unrealized_futures_usd=usd(unrealized),
        fees_usd=usd(fees),
        income_usd=usd(income),
        hedge_after_fees_usd=usd(realized + unrealized - fees),
        unhedged_value_usd=usd(base + income + equity_value),
        opening_displayed_account_usd=seed["displayed_account_usd"],
        opening_difference_usd=usd(opening - number(seed["displayed_account_usd"])),
        opening_value_usd=usd(opening),
        portfolio_value_usd=usd(nav),
        pnl_since_open_usd=usd(nav - opening),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="paper-portfolio.sqlite3", help="path to the working ledger"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "history"):
        commands.add_parser(name).add_argument(
            "--as-of", help="inclusive timestamp with UTC offset"
        )
    research = commands.add_parser(
        "research", help="build a decision input from saved research and the book"
    )
    research.add_argument("gdelt_dir", type=Path)
    research.add_argument("analysts", type=Path)
    research.add_argument("--as-of", help="decision time with UTC offset (default: now)")
    research.add_argument(
        "--allow-fixture", action="store_true", help="allow a labelled synthetic GDELT rehearsal"
    )
    commands.add_parser("record").add_argument("file", help="one event JSON file, or - for stdin")
    args = parser.parse_args()
    with closing(sqlite3.connect(args.db, timeout=15)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        seed = open_book(db)
        if args.command == "record":
            raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
            event = json.loads(raw, parse_float=str)
            result = {"id": event["id"], **record(db, seed, event)}
        else:
            cutoff = (
                instant(args.as_of) if args.as_of else datetime.max.replace(tzinfo=timezone.utc)
            )
            if args.command == "research" and not args.as_of:
                cutoff = datetime.now(timezone.utc)
            if cutoff < instant(seed["at"]):
                raise ValueError("Requested date precedes the opening portfolio.")
            events = entries(db, cutoff.isoformat())
            result = events if args.command == "history" else statement(seed, events)
            if args.command == "research":
                from paired_research import paired_research

                result = paired_research(
                    args.gdelt_dir, args.analysts, result, cutoff, args.allow_fixture
                )
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        sqlite3.Error,
        InvalidOperation,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        sys.exit(f"Ledger: {exc}")
