"""Prospective paper evaluations. Both scoring marks must occur after the decision is recorded."""

import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import timezone
from itertools import islice
from pathlib import Path

from ledger import EASTERN, MULTIPLIER, instant, number, usd


def location(output):
    return Path(os.environ.get("HEDGE_HISTORY_DB") or Path(output) / "decision-history.sqlite3")


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, decided TEXT, day TEXT, contract TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS marks(contract TEXT, observed TEXT, available TEXT, day TEXT, price TEXT, basis TEXT,
            PRIMARY KEY(contract, observed, available));
        CREATE TABLE IF NOT EXISTS starts(id TEXT PRIMARY KEY, available TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS outcomes(id TEXT PRIMARY KEY, available TEXT, data TEXT);
    """)
    return db


def remember_mark(db, market, available):
    if not market or market.get("synthetic"):
        return
    contract = market["contract"]
    q = market["quotes"][contract]
    close = market.get("price_mode") == "last_close_rehearsal"
    if close and (q.get("mode") != "HISTORICAL_CLOSE" or not q.get("is_close")):
        return
    if not close and (
        q.get("mode") != "REALTIME"
        or q.get("is_close") is not False
        or q.get("halted") is not False
    ):
        return
    observed = q.get("valuation_at" if close else "observed_at")
    if not observed or not q.get("received_at"):
        return
    stamp, received = instant(observed), instant(q["received_at"])
    if stamp > received or received > available or number(q["last"]) <= 0:
        return
    db.execute(
        "INSERT OR IGNORE INTO marks VALUES (?,?,?,?,?,?)",
        (
            contract,
            stamp.isoformat(),
            received.isoformat(),
            stamp.astimezone(EASTERN).date().isoformat(),
            str(q["last"]),
            "IBKR session close" if close else "IBKR live observation",
        ),
    )


def archive(db, folder):
    def read(name):
        return json.loads((folder / name).read_bytes())

    memo = read("decision.json")
    key = memo.get("response_id")
    if not key or memo.get("mode") not in ("Astra close rehearsal", "Astra advisory"):
        return
    if db.execute("SELECT 1 FROM decisions WHERE id=?", (key,)).fetchone():
        return
    if memo["mode"] == "Astra close rehearsal" and not (folder / "paper-receipt.json").exists():
        return
    p = read("request.json")
    if p["market"].get("synthetic"):
        return
    when = max(
        instant(memo["completed_at"]), instant(memo.get("recorded_at", memo["completed_at"]))
    )
    market, table = p["market"], p["alternatives"]
    contract = market["contract"]
    target, before = memo["target_contracts"], memo["current_contracts"]
    choice = next(r for r in table["candidates"] if r["target_contracts"] == target)
    fee, entry = choice["change_cost_usd"], market["quotes"][contract]["last"]
    basis = "Unfilled advisory study"
    if memo["mode"] == "Astra close rehearsal":
        result = read("paper-receipt.json")["result"]
        if memo["action"] == "adjust" and result["status"] != "filled":
            raise ValueError("A paper adjustment requires its simulator fill receipt.")
        if result.get("fill_price") is not None:
            entry = result["fill_price"]
        basis = "Paper decision study"
    day = (
        market.get("session_date")
        or p.get("intended_session")
        or instant(p["as_of"]).astimezone(EASTERN).date().isoformat()
    )
    price_session = market.get("session_date")
    if p.get("prior_paper_decision"):
        day = instant(p["as_of"]).astimezone(EASTERN).date().isoformat()
    freshness = p.get("source_freshness")
    if freshness is None and "analysts" in p:
        from source_dates import source_dates

        freshness = source_dates(p)
    row = {
        "model": memo["model"],
        "prompt_sha256": memo.get("prompt_sha256"),
        "input_sha256": p["input_sha256"],
        "decision_cutoff": p["as_of"],
        "horizon": memo.get("horizon"),
        "model_effort": memo.get("reasoning_effort"),
        "source_warnings": [
            r["name"]
            for r in (freshness or {}).get("rows", [])
            if r["status"] not in ("Current", "Replay close", "Reference", "Dated")
        ],
        "candidate_costs": {
            str(v["target_contracts"]): v["change_cost_usd"]
            for v in table["candidates"]
            if v.get("eligible_target", True) or v["target_contracts"] == before
        },
        "id": key,
        "time": when.isoformat(),
        "session": day,
        "price_session": price_session,
        "basis": basis,
        "contract": contract,
        "previous": before,
        "target": target,
        "action": memo["action"],
        "reference_price": str(entry),
        "fee_usd": str(fee),
        "rationale": memo["rationale"],
        "countercase": memo.get("countercase"),
        "reconsider_if": memo.get("reconsider_if"),
        "analyst_run": p["analyst_run"],
        "forecast_ids": memo["forecast_ids"],
        "gdelt_run_id": memo.get("gdelt_run_id"),
    }
    db.execute(
        "INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?)",
        (key, when.isoformat(), day, contract, json.dumps(row)),
    )
    remember_mark(db, market, when)


def settle(db, now):
    pending = db.execute(
        """SELECT d.id,d.contract,d.decided,d.data,s.data FROM decisions d
        LEFT JOIN starts s ON d.id=s.id LEFT JOIN outcomes o ON d.id=o.id
        WHERE o.id IS NULL AND d.decided<=?""",
        (now,),
    ).fetchall()
    for key, contract, recorded, raw, started in pending:
        if started:
            start = json.loads(started)
        else:
            first = db.execute(
                """SELECT observed,available,day,price,basis FROM marks WHERE contract=?
                AND observed>? AND available<=? ORDER BY available,observed LIMIT 1""",
                (contract, recorded, now),
            ).fetchone()
            if first is None:
                continue
            start = {
                "observed": first[0],
                "available": first[1],
                "day": first[2],
                "price": first[3],
                "basis": first[4],
            }
            db.execute("INSERT INTO starts VALUES (?,?,?)", (key, first[1], json.dumps(start)))
        mark = db.execute(
            """SELECT observed,available,price,basis FROM marks WHERE contract=? AND day>?
            AND observed>? AND available<=? ORDER BY available,observed LIMIT 1""",
            (contract, start["day"], start["available"], now),
        ).fetchone()
        if mark is None:
            continue
        row = json.loads(raw)
        move = (number(mark[2]) - number(start["price"])) * MULTIPLIER
        fee = number(row["fee_usd"])
        effect = (row["target"] - row["previous"]) * move - fee
        out = {
            "observed": mark[0],
            "available": mark[1],
            "exit_mark": mark[2],
            "mark_basis": mark[3],
            "versus_unhedged_usd": usd(row["target"] * move - fee),
            "versus_keep_usd": usd(effect),
            "result": "Helped" if effect > 0 else "Cost" if effect < 0 else "Neutral",
        }
        out["alternatives"] = {
            target: usd((int(target) - row["previous"]) * move - number(cost))
            for target, cost in row.get("candidate_costs", {}).items()
        }
        db.execute("INSERT INTO outcomes VALUES (?,?,?)", (key, mark[1], json.dumps(out)))


def history(path, as_of, market=None, folder=None, limit=24):
    """Use only decisions and follow-up marks available by as_of. Recover completed prior runs on first read."""
    cutoff = as_of.astimezone(timezone.utc).isoformat()
    with closing(database(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        archives = [
            *path.parent.glob("closing-rehearsals/*/decision.json"),
            *path.parent.glob("live/*/decision.json"),
        ]
        if folder is not None and (folder / "decision.json").exists():
            archives.append(folder / "decision.json")
        for file in sorted(set(archives)):
            archive(db, file.parent)
        remember_mark(db, market, as_of)
        settle(db, cutoff)
        rows = db.execute(
            """SELECT d.data,s.data,o.data FROM decisions d
            LEFT JOIN starts s ON d.id=s.id AND s.available<=?
            LEFT JOIN outcomes o ON d.id=o.id AND o.available<=?
            WHERE d.decided<=? ORDER BY d.decided DESC,d.id LIMIT ?""",
            (cutoff, cutoff, cutoff, limit),
        )
        return [
            json.loads(row)
            | {
                "study_start": json.loads(start) if start else None,
                "outcome": json.loads(out) if out else None,
            }
            for row, start, out in rows
        ]


def model_experience(path, now, market):
    rows = history(path, now, market, limit=-1)
    counts = Counter(row["outcome"]["result"] if row["outcome"] else "Pending" for row in rows)
    examples = rows[:24]
    for result in ("Helped", "Cost", "Neutral"):
        older = (row for row in rows[24:] if row["outcome"] and row["outcome"]["result"] == result)
        examples.extend(list(islice(older, 2)))
    return {
        "prior_decision_outcomes": examples,
        "prior_record_counts": {"total": len(rows), **counts},
    }


def complete(folder, path, now):
    from hedge_session import save

    save(
        folder / "decision-history.json",
        {"as_of": now.isoformat(), "decisions": history(path, now, folder=folder, limit=-1)},
    )
