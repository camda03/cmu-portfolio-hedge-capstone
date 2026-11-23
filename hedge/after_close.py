"""Rehearse a hedge on IBKR daily closes using an isolated copy of the paper book."""

import argparse
import fcntl
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from closing_bars import close_market
from decision_history import complete, location, model_experience
from source_dates import source_dates
from demo_view import gdelt_view, render, tracker
from hedge_session import (
    MEMO_FIELDS,
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
from paired_research import archived_analysts, gdelt_input


def research(args, now):
    if args.archive is None:
        require(args.batch, "Supply the session date printed on the daily producer receipt.")
        receipt, feed = daily_packet(args.receipt, args.batch, now, args.packet)
        require(
            not feed.get("synthetic"),
            "Use a published analyst packet for the closing-price rehearsal.",
        )
        return receipt, feed, "Dated producer receipt"
    require(
        not args.batch and not args.packet, "Batch and packet options belong to a daily receipt."
    )
    feed, digest = archived_analysts(args.archive, now)
    require(
        "session_date" not in feed and not feed.get("synthetic"),
        "Use the dated receipt for a daily packet.",
    )
    issued = instant(feed["generated_at"])
    ids = []
    for item in feed["subjects"].values():
        f = item["forecast"]
        ids.append(f["forecast_id"])
        require(
            instant(f["information_cutoff"]) <= instant(f["issued_at"]) <= issued
            and date.fromisoformat(f["horizon_end"]) >= now.astimezone(EASTERN).date()
            and number(f["reference_price"]) > 0
            and number(f["expected_end_price"]) >= 0
            and number(f["expected_return"]) >= -1,
            "Unusable archived forecast.",
        )
        item["report_sha256"] = sha256(item["report_markdown"].encode()).hexdigest()
    require(len(set(ids)) == 7, "Duplicate archived forecast IDs.")
    return (
        dict(
            session_date=issued.astimezone(EASTERN).date().isoformat(),
            run_id=feed["fleet"]["test_id"],
            packet_key=args.archive.name,
            packet_sha256=digest,
            source_revision=None,
        ),
        feed,
        "Archived seven-report export",
    )


def control_response(inputs, target):
    current = inputs["alternatives"]["current_contracts"]
    memo = dict.fromkeys(
        MEMO_FIELDS, "Scripted plumbing check. No investment view was requested from a model."
    )
    memo.update(
        horizon="One accounting rehearsal",
        rationale="Operator-selected contract count exercises the paper ledger.",
        action="hold" if target == current else "adjust",
        target_contracts=target,
        forecast_ids=[a["forecast"]["forecast_id"] for a in inputs["analysts"].values()],
        gdelt_run_id=inputs["gdelt"]["run_id"],
    )
    return dict(
        status="completed",
        model="Scripted test response",
        id=None,
        usage=None,
        output_text=json.dumps(memo),
    )


def paper_decision(db, seed, inputs, decision):
    at, market = inputs["as_of"], inputs["market"]
    event = dict(
        kind=decision["action"],
        id="closing-rehearsal-decision-" + instant(at).astimezone(EASTERN).date().isoformat(),
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
    review = read_json(args.review) if getattr(args, "review", None) else None
    book = args.book
    if review:
        # A follow-up decision must use the named evidence and the exact book left by its predecessor.
        require(
            not book
            and (receipt["run_id"], receipt["packet_sha256"], gdelt["run_id"], gdelt_hash)
            == (
                review["analyst_run"],
                review["analyst_sha256"],
                review["gdelt_run"],
                review["gdelt_sha256"],
            ),
            "The analyst and GDELT publications must match the selected review.",
        )
        matches = [
            file.parent
            for file in (args.out / "closing-rehearsals").glob("*/decision.json")
            if read_json(file).get("response_id") == review["prior_response_id"]
        ]
        require(len(matches) == 1, "Expected one completed predecessor on this worker.")
        previous = matches[0]
        prior_request, prior_memo = (
            read_json(previous / "request.json"),
            read_json(previous / "decision.json"),
        )
        require(
            prior_memo["mode"] == "Astra close rehearsal"
            and prior_memo["target_contracts"] == review["prior_target"]
            and instant(prior_memo["completed_at"]) <= now
            and prior_request["batch_date"] < receipt["session_date"]
            and market["session_date"]
            == review["price_session"]
            == prior_request["market"]["session_date"]
            and market["capture_sha256"] == prior_request["market"]["capture_sha256"]
            and policy == prior_request["policy"],
            "The prior decision, IBKR closing capture or policy differs from the selected review.",
        )
        book = previous / "paper.sqlite3"
    with closing(sqlite3.connect(":memory:")) as db:
        if book:
            with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                require(
                    source.execute("SELECT 1 FROM journal WHERE id='opening'").fetchone(),
                    "Expected an existing paper book.",
                )
                source.backup(db)
        seed = open_book(db)
        prior = entries(db, datetime.max.replace(tzinfo=timezone.utc).isoformat())
        before = statement(seed, prior)
        close_at = min(instant(q["valuation_at"]) for q in market["quotes"].values())
        if review:
            require(
                before == read_json(previous / "tracker.json")["current"]
                and before["futures"].get(market["contract"], {}).get("contracts", 0)
                == review["prior_target"],
                "The predecessor's paper journal must reproduce its published closing position.",
            )
        require(
            set(before["equities"]) == STOCKS
            and instant(before["as_of"]) <= (now if review else close_at)
            and all(
                s == market["contract"] or not p["contracts"] for s, p in before["futures"].items()
            ),
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
        inputs = prepare(
            receipt, feed, gdelt, gdelt_hash, statement(seed, marked), market, policy, now
        )
        if review:
            inputs["prior_paper_decision"] = review["prior_response_id"]
            inputs["snapshot_asof_label"] = (
                f"Reviewed {now.astimezone(EASTERN):%b %d, %Y %I:%M %p %Z} · "
                f"IBKR closing prices {market['session_date']}"
            )
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
            "astra" if args.astra else args.test_target,
            sha256(prompt.encode()).hexdigest(),
        ]
        if review:
            basis.append(review["prior_response_id"])
        key = sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:20]
        folder = args.out / "closing-rehearsals" / (market["session_date"] + "-" + key)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "session.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (folder / "request.json").exists():
                inputs = read_json(folder / "request.json")
            else:
                inputs["source_freshness"] = source_dates(inputs)
                if args.astra:
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
                if args.astra:
                    complete(folder, location(args.out), now)
                render(folder)
                return folder
            if args.astra:
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
            else:
                decision = reviewed_response(
                    inputs, control_response(inputs, args.test_target), prompt
                )
                decision.update(mode="Scripted close rehearsal", reasoning_effort=None)
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
            if args.astra:
                complete(folder, location(args.out), datetime.now(timezone.utc))
            render(folder)
            return folder


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--archive", type=Path, help="older, explicitly archived seven-report export"
    )
    source.add_argument("--receipt", help="dated completed status.json, local or gs://")
    p.add_argument("--batch", help="date on the producer receipt")
    p.add_argument(
        "--packet", type=Path, help="local packet path if different from the receipt's sibling"
    )
    p.add_argument(
        "--book",
        type=Path,
        help="read-only source paper ledger; defaults to the course opening book",
    )
    p.add_argument("--policy", type=Path, default=ROOT / "demo_assets/pilot.example.json")
    p.add_argument(
        "--review",
        type=Path,
        help="approved research pair and predecessor for a same-close paper review",
    )
    for name in ("bars", "gdelt", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--test-target", type=int, help="scripted signed target to test paper accounting"
    )
    mode.add_argument(
        "--astra", action="store_true", help="ask the configured Astra service for the judgment"
    )
    print(run(p.parse_args()) / "index.html")


if __name__ == "__main__":
    os.umask(0o077)
    main()
