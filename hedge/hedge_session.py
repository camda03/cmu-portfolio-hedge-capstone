"""Prepare one dated, advisory Astra decision from two research packets and a simulated book."""

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from decision_history import complete, location, model_experience
from source_dates import source_dates
from hedge_math import CALCULATOR, calculator_call, evaluate_hedges
from ledger import EASTERN, MES, entries, instant, number, open_book, record, statement
from paired_research import gdelt_input, require

ROOT = Path(__file__).resolve().parent
STOCKS = {"AAPL", "DE", "DELL", "MSFT", "NVDA", "WMT"}
SUBJECTS = STOCKS | {"SP500"}
MODEL = "gpt-6.2-astra"
RESEARCH_STORE = os.environ.get("HEDGE_RESEARCH_STORE", "")
DASHBOARD = os.environ.get("RESEARCH_DASHBOARD_URL", "").rstrip("/")
MEMO_FIELDS = (
    "horizon",
    "market_view",
    "basket_view",
    "price_interpretation",
    "evidence_weight",
    "rationale",
    "countercase",
    "reconsider_if",
)
OUTPUT_FIELDS = {
    "action": {"type": "string", "enum": ["adjust", "hold", "defer"]},
    "target_contracts": {"type": "integer"},
    **{k: {"type": "string"} for k in MEMO_FIELDS},
    "forecast_ids": {"type": "array", "items": {"type": "string"}},
    "gdelt_run_id": {"type": ["string", "null"]},
}
SCHEMA = {
    "type": "object",
    "properties": OUTPUT_FIELDS,
    "required": list(OUTPUT_FIELDS),
    "additionalProperties": False,
}


def read_json(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    """Atomic private file replacement in its destination directory."""
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(value, out, indent=2, allow_nan=False)
            out.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def object_bytes(location):
    if str(location).startswith("gs://"):
        # On the worker the storage client uses its attached service identity.
        # Keep the existing authenticated CLI available on operator workstations.
        try:
            from google.auth.exceptions import DefaultCredentialsError
            from google.cloud import storage
        except ImportError:
            if not shutil.which("gcloud"):
                raise
        else:
            uri = urlsplit(str(location))
            try:
                client = storage.Client()
            except DefaultCredentialsError:
                if not shutil.which("gcloud"):
                    raise
            else:
                return (
                    client.bucket(uri.netloc)
                    .blob(uri.path.lstrip("/"))
                    .download_as_bytes(timeout=30)
                )
        return subprocess.run(
            ["gcloud", "storage", "cat", str(location)], check=True, stdout=subprocess.PIPE
        ).stdout
    return Path(location).read_bytes()


def check_receipt(receipt, batch, cutoff):
    run = receipt["run_id"]
    require(
        receipt["status"] == "completed"
        and receipt["accepted"] == 7
        and receipt["session_date"] == batch
        and receipt["packet_key"] == f"portfolio-research/{run}.json"
        and instant(receipt["updated_at"]) <= cutoff,
        "Expected a completed, dated seven-report receipt as of this decision.",
    )


def worker_delivery(day, cutoff, account):
    """Use the VM's attached identity for both reads, including an authorized storage fallback."""
    from google.auth.compute_engine import Credentials
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.cloud import storage

    require(
        DASHBOARD and RESEARCH_STORE.startswith("gs://"),
        "Configure RESEARCH_DASHBOARD_URL and HEDGE_RESEARCH_STORE for worker delivery.",
    )

    def attached(scopes):
        credentials = Credentials(scopes=scopes)
        credentials.refresh(Request())
        require(
            credentials.service_account_email == account,
            "The attached service account differs from the hedge account.",
        )
        return credentials

    def packet_key(receipt):
        check_receipt(receipt, day, cutoff)
        run = receipt["run_id"]
        require(
            re.fullmatch(r"securities-[0-9]{8}T[0-9]{6}Z", run)
            and receipt["dashboard_url"] == f"{DASHBOARD}/api/portfolio-research/test/{run}",
            "The receipt must identify the immutable packet on the configured dashboard.",
        )
        return receipt["packet_key"]

    meta = {
        "service_account": account,
        "host": socket.gethostname(),
        "transport": "dashboard_https",
    }
    status_url = f"{DASHBOARD}/api/daily-research/{day}/status"
    try:
        credentials = attached(["openid", "https://www.googleapis.com/auth/userinfo.email"])
    except RefreshError:
        meta["dashboard_access"] = "scoped_token_unavailable"
    else:
        with AuthorizedSession(credentials) as session:

            def get(url):
                return session.get(
                    url, headers={"Accept": "application/json"}, allow_redirects=False, timeout=30
                )

            response = get(status_url)
            if response.status_code == 200:
                receipt = response.json()
                packet_key(receipt)
                response = get(receipt["dashboard_url"])
                if response.status_code == 200:
                    return receipt | {"hedge_read": meta}, response.content
            require(
                response.status_code in (302, 303, 401, 403),
                f"Dashboard returned HTTP {response.status_code}.",
            )
            meta["dashboard_access"] = response.status_code

    # The existing VM account already reads the two originals from the producer's bucket.
    credentials = attached(["https://www.googleapis.com/auth/devstorage.read_only"])
    bucket = storage.Client(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"), credentials=credentials
    ).bucket(urlsplit(RESEARCH_STORE).netloc)
    receipt = json.loads(
        bucket.blob(f"daily-research/{day}/status.json").download_as_bytes(timeout=30)
    )
    raw = bucket.blob(packet_key(receipt)).download_as_bytes(timeout=30)
    return receipt | {"hedge_read": meta | {"transport": "cloud_storage"}}, raw


def research_files(receipt_location, batch, cutoff, packet_file):
    if receipt_location is None:
        day = date.fromisoformat(batch).isoformat()
        bucket = os.environ.get("HEDGE_RESEARCH_STORE", RESEARCH_STORE).rstrip("/")
        uri = urlsplit(bucket)
        require(
            uri.scheme == "gs" and uri.netloc and not (uri.path or uri.query or uri.fragment),
            "Set HEDGE_RESEARCH_STORE to a gs:// bucket.",
        )
        receipt_location = f"{bucket}/daily-research/{day}/status.json"
    receipt = json.loads(object_bytes(receipt_location))
    check_receipt(receipt, batch, cutoff)
    if str(receipt_location).startswith("gs://"):
        bucket = str(receipt_location).split("/", 3)[:3]
        require(not packet_file, "GCS mode reads the exact object named in the receipt.")
        packet_file = "/".join(bucket) + "/" + receipt["packet_key"]
    else:
        packet_file = packet_file or Path(receipt_location).with_name("portfolio-research.json")
    return receipt, object_bytes(packet_file)


def daily_packet(receipt_location, batch, cutoff, packet_file=None):
    account = os.environ.get("HEDGE_WORKER_ACCOUNT")
    if account:
        require(
            receipt_location is None and packet_file is None,
            "The hosted hedge worker reads the dated publication.",
        )
        require(
            date.fromisoformat(batch).isoformat() == batch,
            "Use an ISO calendar date for the batch.",
        )
        receipt, raw = worker_delivery(batch, cutoff, account)
    else:
        receipt, raw = research_files(receipt_location, batch, cutoff, packet_file)
    require(sha256(raw).hexdigest() == receipt["packet_sha256"], "Daily packet checksum mismatch.")
    feed = json.loads(raw)
    if account:
        require(not feed.get("synthetic"), "The hosted check requires published research.")
    run = receipt["run_id"]
    generated = instant(feed["generated_at"])
    require(
        feed["schema"] == "portfolio-research-v1"
        and feed["run_id"] == feed["fleet"]["test_id"] == run
        and feed["session_date"] == batch
        and feed["source_revision"] == receipt["source_revision"]
        and feed["all_reports_available"] is True
        and feed["execute"] is False
        and feed["hourly_monitoring"] is False
        and set(feed["subjects"]) == SUBJECTS
        and generated <= cutoff,
        "Invalid or mismatched daily research packet.",
    )
    ids = []
    for symbol, subject in feed["subjects"].items():
        f = subject["forecast"]
        body = subject["report_markdown"]
        require(
            subject["status"] == "available"
            and subject["symbol"] == f["symbol"] == symbol
            and f["forecast_id"]
            and body.strip()
            and sha256(body.encode()).hexdigest() == subject["report_sha256"]
            and instant(f["information_cutoff"]) <= instant(f["issued_at"]) <= generated,
            f"Report integrity, identity or timestamp failed for {symbol}.",
        )
        require(
            (f["status"] == "abstain" and str(f.get("reason") or "").strip())
            or (
                f["status"] == "estimate"
                and number(f["reference_price"]) > 0
                and number(f["expected_return"]) >= -1
                and number(f["expected_end_price"]) >= 0
                and date.fromisoformat(f["horizon_end"]) >= cutoff.astimezone(EASTERN).date()
            ),
            f"Missing or expired forecast for {symbol}.",
        )
        ids.append(f["forecast_id"])
    require(len(set(ids)) == 7, "Duplicate forecast IDs.")
    return receipt, feed


def session_hours(now, session):
    import pandas_market_calendars as mcal

    require(
        now.astimezone(EASTERN).date().isoformat() == session,
        "The requested New York session is not today.",
    )
    hours = mcal.get_calendar("NYSE").schedule(start_date=session, end_date=session)
    require(not hours.empty, "No NYSE trading session on this date.")
    start, end = hours.iloc[0][["market_open", "market_close"]]
    require(start <= now < end, "Outside the regular New York trading session.")
    return start.to_pydatetime(), end.to_pydatetime()


def marked_book(db, seed, market, cutoff, opening, max_age):
    require(
        market.get("price_mode") != "last_close_rehearsal",
        "Historical closes cannot enter a live-price session.",
    )
    contract = market["contract"]
    quotes = market["quotes"]
    require(
        MES.fullmatch(contract) and set(quotes) == SUBJECTS | {contract} and 0 < number(max_age),
        "Eight instrument marks and a positive quote-age limit are required.",
    )
    as_of = instant(market["captured_at"])
    require(opening <= as_of <= cutoff, "Market capture is outside the decision session.")
    prior = entries(db, cutoff.isoformat())
    require(instant(seed["at"]) <= cutoff, "The opening book postdates this decision.")
    book = statement(seed, prior)
    require(
        set(book["equities"]) == STOCKS
        and all(k == contract or not p["contracts"] for k, p in book["futures"].items()),
        "Another expiry is open or the fixed six-stock basket differs. Review the book before sizing.",
    )
    valuation = []
    for symbol, q in quotes.items():
        observed = instant(q["observed_at"])
        require(
            q["mode"] == "REALTIME"
            and q["halted"] is False
            and q["is_close"] is False
            and opening <= observed <= instant(q["received_at"]) <= as_of
            and (cutoff - observed).total_seconds() <= max_age
            and number(q["last"]) > 0,
            f"Missing, delayed or stale decision mark for {symbol}.",
        )
        if symbol != "SP500":
            held = book["equities"].get(symbol) or book["futures"].get(symbol)
            require(
                not held or instant(held["marked_at"]) <= observed,
                f"Newer ledger mark exists for {symbol}.",
            )
            valuation.append(
                dict(
                    kind="mark",
                    at=q["observed_at"],
                    source=market["source"],
                    prices={symbol: q["last"]},
                    receipt={"status": "marked"},
                )
            )
    # Value the snapshot in memory. The review caller persists the marks separately from any paper trade.
    events = sorted(prior + valuation, key=lambda event: instant(event["at"]))
    return statement(seed, events), valuation


def prepare(receipt, feed, gdelt, gdelt_hash, book, market, policy, cutoff):
    contract = market["contract"]
    targets = policy["targets"]
    require(
        isinstance(targets, list) and targets and all(type(n) is int for n in targets),
        "Supply the permitted ending positions as signed whole contracts.",
    )
    scenarios = policy["scenarios"]
    require(
        all(k in scenarios for k in ("rally", "flat", "decline")),
        "Include rally, flat and decline scenarios.",
    )
    table = evaluate_hedges(
        book,
        contract,
        market["quotes"][contract]["last"],
        targets,
        policy["cost_per_contract_usd"],
        scenarios,
        policy.get("betas"),
    )
    if policy.get("betas"):
        require(
            str(policy.get("beta_source") or "").strip(),
            "State the source and as-of date of the beta assumptions.",
        )
    bounds = policy.get("beta_bounds")
    if bounds is not None:
        require(
            len(bounds) == 2
            and number(bounds[0]) <= number(bounds[1])
            and not table["missing_betas"],
            "Beta limits require ordered bounds and six dated beta estimates.",
        )
    for row in table["candidates"]:
        row["eligible_target"] = row["target_contracts"] in targets and (
            bounds is None or number(bounds[0]) <= number(row["net_beta"]) <= number(bounds[1])
        )
    analysts = {
        s: {
            k: item[k]
            for k in ("report_markdown", "report_sha256", "report_key", "forecast", "coverage_gaps")
        }
        for s, item in feed["subjects"].items()
    }
    return dict(
        as_of=cutoff.isoformat(),
        batch_date=receipt["session_date"],
        analyst_run=receipt["run_id"],
        analyst_revision=receipt["source_revision"],
        packet_key=receipt["packet_key"],
        input_sha256={"analysts": receipt["packet_sha256"], "gdelt": gdelt_hash},
        analysts=analysts,
        gdelt=gdelt,
        market=market,
        portfolio=book,
        policy=policy,
        alternatives=table,
        **({"research_delivery": receipt["hedge_read"]} if "hedge_read" in receipt else {}),
    )


def model_payload(inputs):
    """Represent repeated search URL annotations once across the original coverage maps."""
    wire = deepcopy(inputs)
    trace_groups = []
    membership = defaultdict(list)
    for analyst in wire["analysts"].values():
        discovery = analyst["forecast"].get("shared_discovery") or {}
        for stage in (discovery, *discovery.get("components", [])):
            coverage = stage.get("coverage") or {}
            trace = coverage.get("source_trace")
            if not isinstance(trace, dict) or not trace:
                continue
            for url, tags in trace.items():
                membership[url, json.dumps(tags, sort_keys=True)].append(len(trace_groups))
            group_ids = []
            trace_groups.append(group_ids)
            coverage["source_trace"] = {"groups": group_ids}

    # One group has the same annotation and belongs to the same set of searches.
    urls_by_group = defaultdict(list)
    for (url, tags), searches in membership.items():
        urls_by_group[tags, tuple(searches)].append(url)
    if urls_by_group:
        groups = wire["source_trace_groups"] = []
        for (tags, searches), urls in urls_by_group.items():
            for search in searches:
                trace_groups[search].append(len(groups))
            groups.append({"tags": json.loads(tags), "urls": urls})
    return wire


def model_review(inputs, client, response_path=None):
    prompt = (ROOT / "hedge_prompt.md").read_text()
    research = json.dumps(
        model_payload(inputs), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    dialogue = [{"role": "user", "content": research}]
    calls, turns = [], []
    for round_number in range(4):
        reply = client.responses.create(
            model=MODEL,
            reasoning={"effort": "high"},
            store=False,
            instructions=prompt,
            input=research if round_number == 0 else dialogue,
            tools=[CALCULATOR],
            tool_choice="auto" if round_number < 3 else "none",
            parallel_tool_calls=False,
            include=["reasoning.encrypted_content"],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "portfolio_hedge",
                    "strict": True,
                    "schema": SCHEMA,
                }
            },
        )
        require(reply.status == "completed", "Incomplete model turn.")
        turns.append({"id": reply.id, "usage": reply.usage.model_dump() if reply.usage else None})
        output = getattr(reply, "output", [])
        requests = [item for item in output if item.type == "function_call"]
        if not requests:
            break
        require(round_number < 3 and len(requests) == 1, "Unexpected number of calculator calls.")
        call = requests[0]
        require(call.name == CALCULATOR["name"], "Unknown hedge tool.")
        try:
            value = calculator_call(inputs, call.arguments)
        except (ValueError, TypeError, ArithmeticError, KeyError) as exc:
            value = {"error": str(exc)}
        calls.append(
            {"id": call.call_id, "name": call.name, "arguments": call.arguments, "result": value}
        )
        # Stateless Responses carries the provider's encrypted reasoning item through this conversation only.
        dialogue.extend(output)
        dialogue.append(
            {
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(value, allow_nan=False, separators=(",", ":")),
            }
        )
    total_usage = None
    if all(turn["usage"] for turn in turns):
        total_usage = {
            key: sum(turn["usage"].get(key, 0) or 0 for turn in turns)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
    response = dict(
        id=reply.id,
        model=reply.model,
        status=reply.status,
        output_text=reply.output_text,
        usage=turns[-1]["usage"],
        total_usage=total_usage,
        model_turns=turns,
        calculator_calls=calls,
    )
    if response_path is not None:
        save(response_path, response)
    return reviewed_response(inputs, response, prompt)


def reviewed_response(inputs, response, prompt):
    require(
        response["status"] == "completed" and response["output_text"], "Incomplete model response."
    )
    memo = json.loads(response["output_text"])
    require(
        set(memo) == set(OUTPUT_FIELDS)
        and all(isinstance(memo[k], str) and memo[k].strip() for k in MEMO_FIELDS),
        "Incomplete decision memo.",
    )
    table = inputs["alternatives"]
    target, action = memo["target_contracts"], memo["action"]
    current = table["current_contracts"]
    permitted = [r["target_contracts"] for r in table["candidates"] if r["eligible_target"]]
    require(
        type(target) is int
        and (
            (action in ("hold", "defer") and target == current)
            or (action == "adjust" and target in permitted and target != current)
        ),
        "Action and ending position are inconsistent or outside policy.",
    )
    known = {a["forecast"]["forecast_id"] for a in inputs["analysts"].values()}
    refs = memo["forecast_ids"]
    require(
        isinstance(refs, list)
        and refs
        and set(refs) <= known
        and memo["gdelt_run_id"] == (inputs["gdelt"] or {}).get("run_id"),
        "Decision cites unknown or missing research.",
    )
    return dict(
        **memo,
        current_contracts=current,
        change_contracts=target - current,
        position_review_required=target not in permitted,
        model=response["model"],
        response_id=response["id"],
        reasoning_effort="high",
        usage=response["usage"],
        model_turns=response.get("model_turns", []),
        total_usage=response.get("total_usage"),
        calculator_calls=response.get("calculator_calls", []),
        prompt_sha256=sha256(prompt.encode()).hexdigest(),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="verify the analyst publication at any hour")
    review = sub.add_parser(
        "review", help="collect fresh IBKR marks and obtain one advisory Astra memo"
    )
    for cmd in (check, review):
        cmd.add_argument(
            "--receipt", help="override the default dated bucket receipt with a gs:// or local path"
        )
        cmd.add_argument("--batch", required=True, help="explicit research batch date")
        cmd.add_argument(
            "--packet",
            type=Path,
            help="local packet path when different from the receipt's sibling",
        )
    source = review.add_mutually_exclusive_group(required=True)
    source.add_argument("--gdelt", type=Path, help="completed GDELT v2 directory")
    source.add_argument(
        "--gdelt-gap", help="document why historical research is unavailable for this session"
    )
    for name in ("db", "policy", "broker-dir", "out"):
        review.add_argument("--" + name, type=Path, required=True)
    review.add_argument("--session", required=True, help="intended New York trading date")
    args = parser.parse_args()
    clock = lambda: datetime.now(timezone.utc)
    receipt, feed = daily_packet(args.receipt, args.batch, clock(), args.packet)
    if args.command == "check":
        result = {
            "run_id": receipt["run_id"],
            "session_date": receipt["session_date"],
            "sha256": receipt["packet_sha256"],
            "reports": len(feed["subjects"]),
            "forecasts": {
                s: [v["forecast"]["forecast_id"], v["forecast"]["information_cutoff"]]
                for s, v in feed["subjects"].items()
            },
        }
        if os.environ.get("HEDGE_WORKER_ACCOUNT"):
            result.update(
                receipt["hedge_read"],
                event="hedge_research_verified",
                dashboard_url=receipt["dashboard_url"],
                revision=os.environ.get("HEDGE_WORKER_REVISION"),
                verified_at=clock().isoformat(),
            )
        print(json.dumps(result, indent=2))
        return
    from openai import OpenAI
    from market_snapshot import collect
    from demo_view import gdelt_view, render, tracker
    import fcntl

    session_hours(clock(), args.session)
    import pandas_market_calendars as mcal

    session_day = date.fromisoformat(args.session)
    earlier_sessions = mcal.get_calendar("NYSE").valid_days(
        session_day - timedelta(days=14), session_day - timedelta(days=1)
    )
    require(
        earlier_sessions[-1].date() <= date.fromisoformat(args.batch) <= session_day,
        "Use the fresh research batch designated for this session.",
    )
    if args.gdelt:
        gdelt, digest, _ = gdelt_input(args.gdelt, clock(), False)
    else:
        require(args.gdelt_gap.strip(), "Explain the GDELT coverage gap.")
        gdelt, digest = None, None
    require(not feed.get("synthetic"), "Use the offline rehearsal for synthetic analyst inputs.")
    policy = read_json(args.policy)
    dest = args.out / args.session
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / "session.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (dest / "decision.json").exists():
            previous = read_json(dest / "request.json")
            require(
                previous["input_sha256"] == {"analysts": receipt["packet_sha256"], "gdelt": digest}
                and previous["policy"] == policy
                and previous.get("gdelt_gap") == args.gdelt_gap,
                "The saved decision used different inputs. Reopen its index.html.",
            )
            complete(dest, location(args.out.parent), clock())
            render(dest)
            print(dest / "index.html")
            return
        require(
            not (dest / "request.json").exists(),
            "An earlier model attempt needs reconciliation in this session folder.",
        )
        client = OpenAI(max_retries=0, timeout=1800)
        market = collect(args.broker_dir, policy, dest, args.session)
        cutoff = clock()
        opening, closing_at = session_hours(cutoff, args.session)
        with closing(sqlite3.connect(args.db, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            seed = open_book(db)
            book, marks = marked_book(
                db, seed, market, cutoff, opening, policy["max_mark_age_seconds"]
            )
            for mark in marks:
                symbol = next(iter(mark["prices"]))
                event = {k: v for k, v in mark.items() if k != "receipt"}
                record(db, seed, dict(id=f"ibkr-{symbol}-{mark['at']}", **event))
            tracked = tracker(seed, entries(db, cutoff.isoformat()))
        save(dest / "tracker.json", tracked)
        save(dest / "gdelt-view.json", gdelt_view(args.gdelt, gdelt) if gdelt else None)
        payload = prepare(receipt, feed, gdelt, digest, book, market, policy, cutoff)
        payload["dashboard_url"] = receipt.get("dashboard_url")
        payload["gdelt_gap"] = args.gdelt_gap
        payload["intended_session"] = args.session
        payload["source_freshness"] = source_dates(payload)
        payload.update(model_experience(location(args.out.parent), cutoff, market))
        session_hours(clock(), args.session)
        require(
            all(
                (clock() - instant(q["observed_at"])).total_seconds()
                <= policy["max_mark_age_seconds"]
                for q in market["quotes"].values()
            ),
            "Decision marks expired before dispatch.",
        )
        save(dest / "request.json", payload)
        try:
            memo = model_review(payload, client, dest / "response.json")
        except ValueError as exc:
            save(dest / "review-status.json", {"status": "human_review", "reason": str(exc)})
            raise
        memo.update(
            mode="Astra advisory",
            completed_at=clock().isoformat(),
            session_close=closing_at.isoformat(),
        )
        memo["recorded_at"] = clock().isoformat()
        save(dest / "decision.json", memo)
        complete(dest, location(args.out.parent), clock())
        render(dest)
        print(dest / "index.html")


if __name__ == "__main__":
    os.umask(0o077)
    main()
