"""Rehearse a hedge on IBKR daily closes using an isolated copy of the paper book."""

import argparse
import fcntl
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from closing_bars import close_market
from decision_history import complete, location, model_experience
from source_dates import source_dates
from desk import gdelt_view, render, tracker
from hedge_session import (
    ROOT,
    STOCKS,
    daily_packet,
    model_review,
    prepare,
    read_json,
    require,
    reviewed_response,
    save,
)
from ledger import EASTERN, entries, instant, number, open_book, record, statement, usd
from paired_research import gdelt_input


def research(args, now):
    """Use the published bundle for the specified session, including any recorded abstentions."""
    receipt, feed = daily_packet(args.receipt, args.batch, now, args.packet)
    require(not feed.get("synthetic"), "The closing-price session requires a published research packet.")
    return receipt, feed, "Dated producer receipt"


def paper_decision(db, seed, inputs, decision):
    at, market = inputs["as_of"], inputs["market"]
    event = dict(
        kind=decision["action"],
        id="closing-rehearsal-decision",
        at=at,
        source="Isolated close-price simulator",
        reason=decision["rationale"],
    )
    if decision["action"] == "adjust":
        contract = market["contract"]
        price = market["quotes"][contract]["last"]
        qty = decision["change_contracts"]
        event.update(
            contract=contract,
            contracts=qty,
            fee_usd=usd(abs(qty) * number(inputs["policy"]["cost_per_contract_usd"])),
            quote=dict(
                contract=contract,
                qualified=True,
                bid=price,
                ask=price,
                observed_at=at,
                valid_until=at,
                basis="Simulated zero-spread fill at the historical daily close",
                synthetic=True,
            ),
        )
    result = record(db, seed, event)
    expected = "filled" if decision["action"] == "adjust" else decision["action"]
    require(
        result["status"] == expected and record(db, seed, event) == result,
        "Paper decision or retry failed.",
    )
    return result


def run(args):
    now = datetime.now(timezone.utc)
    receipt, feed, analyst_basis = research(args, now)
    gdelt, gdelt_hash, _ = gdelt_input(args.gdelt, now, False)
    raw = args.bars.read_bytes()
    policy = read_json(args.policy)
    market = close_market(json.loads(raw), policy["future"], now)
    market["capture_sha256"] = sha256(raw).hexdigest()
    market["simulation_assumption"] = (
        "Use the eight historical closing values as a stand-in for market-hour marks. "
        "Use a separate zero-spread paper fill at the MES close, with the stated fee. "
        "All research availability is evaluated at the actual rehearsal time."
    )
    with closing(sqlite3.connect(":memory:")) as db:
        if args.book:
            with closing(sqlite3.connect(args.book.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                require(
                    source.execute("SELECT 1 FROM journal WHERE id='opening'").fetchone(),
                    "Expected an existing paper book.",
                )
                source.backup(db)
        seed = open_book(db)
        prior = entries(db, datetime.max.replace(tzinfo=timezone.utc).isoformat())
        before = statement(seed, prior)
        close_at = min(instant(q["valuation_at"]) for q in market["quotes"].values())
        require(
            set(before["equities"]) == STOCKS
            and instant(before["as_of"]) <= close_at
            and all(s == market["contract"] or not p["contracts"] for s, p in before["futures"].items()),
            "Supply a six-stock paper book as of the close with no other open MES expiry.",
        )
        for symbol, q in market["quotes"].items():
            if symbol != "SP500":
                record(
                    db,
                    seed,
                    dict(
                        kind="mark",
                        id="closing-rehearsal-mark-" + symbol,
                        at=q["valuation_at"],
                        source=market["source"],
                        prices={symbol: q["last"]},
                    ),
                )
        marked = entries(db, now.isoformat())
        inputs = prepare(receipt, feed, gdelt, gdelt_hash, statement(seed, marked), market, policy, now)
        inputs["analyst_publication"] = analyst_basis
        inputs["dashboard_url"] = receipt.get("dashboard_url")
        prompt = (ROOT / "hedge_prompt.md").read_text()
        basis = [
            inputs["input_sha256"],
            market["capture_sha256"],
            policy,
            seed,
            prior,
            analyst_basis,
            market["session_date"],
            "astra",
            sha256(prompt.encode()).hexdigest(),
        ]
        key = sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:20]
        folder = args.out / "closing-rehearsals" / (market["session_date"] + "-" + key)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "session.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (folder / "request.json").exists():
                inputs = read_json(folder / "request.json")
            else:
                inputs["source_freshness"] = source_dates(inputs)
                inputs.update(model_experience(location(args.out), now, market))
                save(folder / "request.json", inputs)
            at = inputs["as_of"]
            require(
                not any(
                    e["kind"] in ("adjust", "hold", "defer")
                    and instant(e["at"]).astimezone(EASTERN).date()
                    == instant(at).astimezone(EASTERN).date()
                    for e in prior
                ),
                "The source paper book already has a decision on the rehearsal day.",
            )
            if (folder / "decision.json").exists() and (folder / "tracker.json").exists():
                complete(folder, location(args.out), now)
                render(folder)
                return folder
            response = folder / "response.json"
            if response.exists():
                decision = reviewed_response(inputs, read_json(response), prompt)
            else:
                from openai import OpenAI

                client = OpenAI(max_retries=0, timeout=1800)
                require(
                    not (folder / "dispatch.json").exists(),
                    "Reconcile the earlier Astra request before retrying.",
                )
                save(folder / "dispatch.json", dict(at=datetime.now(timezone.utc).isoformat()))
                decision = model_review(inputs, client, response)
            decision["mode"] = "Astra close rehearsal"
            decision["completed_at"] = datetime.now(timezone.utc).isoformat()
            result = paper_decision(db, seed, inputs, decision)
            save(folder / "tracker.json", tracker(seed, entries(db, at)))
            save(folder / "gdelt-view.json", gdelt_view(args.gdelt, gdelt))
            save(
                folder / "paper-receipt.json",
                dict(
                    source=market["simulation_assumption"],
                    price_session=market["session_date"],
                    recorded_at=at,
                    result=result,
                ),
            )
            db.commit()
            with TemporaryDirectory(dir=folder) as temporary:
                journal = Path(temporary) / "paper.sqlite3"
                with closing(sqlite3.connect(journal)) as output:
                    db.backup(output)
                journal.replace(folder / "paper.sqlite3")
            decision["recorded_at"] = datetime.now(timezone.utc).isoformat()
            save(folder / "decision.json", decision)
            complete(folder, location(args.out), datetime.now(timezone.utc))
            render(folder)
            return folder


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--receipt", help="dated completed status.json, local or gs://")
    p.add_argument("--batch", required=True, help="date on the producer receipt")
    p.add_argument(
        "--packet", type=Path, help="local packet path if different from the receipt's sibling"
    )
    p.add_argument(
        "--book",
        type=Path,
        help="read-only source paper ledger; defaults to the configured simulated opening book",
    )
    p.add_argument("--policy", type=Path, required=True)
    for name in ("bars", "gdelt", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    print(run(p.parse_args()) / "index.html")


if __name__ == "__main__":
    os.umask(0o077)
    main()
