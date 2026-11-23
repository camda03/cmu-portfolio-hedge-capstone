"""Request eight independent hedge marks over the capstone's authorized IBKR market-data connection."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from hedge_session import SUBJECTS, read_json, require, save, session_hours


def broker_payload(raw):
    sources = [json.loads(c["text"]) for c in raw.get("content", []) if c.get("type") == "text"]
    if raw.get("structuredContent") is not None:
        sources.append(raw["structuredContent"])
    require(
        not raw.get("isError") and sources and all(v == sources[0] for v in sources),
        "Missing or inconsistent broker data.",
    )
    return sources[0]


def normalize(raw, request, received_at):
    data = broker_payload(raw)
    last = data["last"]
    require(type(last["ts"]) in (int, float), "Broker last has no numeric observation time.")
    return dict(
        contract_id=request["contract_id"],
        exchange=request.get("exchange"),
        last=last["price"],
        mode=data["top-status"]["status"],
        halted=last["halted"],
        is_close=last["is_close"],
        observed_at=datetime.fromtimestamp(last["ts"], timezone.utc).isoformat(),
        received_at=received_at,
        reported_bid_ask=data.get("bid-ask"),
        prior_close=data.get("prior-close"),
    )


def collect(broker_dir, policy, output, session):
    broker_dir, output = Path(broker_dir).resolve(), Path(output).resolve()
    catalog = read_json(broker_dir / "dashboard/securities.json")
    future = policy["future"]
    contract = "MES-" + future["contract_month"]
    require(
        future["root"] == "MES"
        and future["exchange"] == "CME"
        and future["multiplier"] == 5
        and type(future["contract_id"]) is int
        and future["contract_id"] > 0
        and future["last_trading_date"].startswith(future["contract_month"])
        and session.replace("-", "") < future["last_trading_date"],
        "Require an exact, unexpired MES contract verified against the broker's futures list.",
    )
    instruments = {s: {"contract_id": catalog[s]["contract_id"]} for s in sorted(SUBJECTS)}
    instruments[contract] = {"contract_id": future["contract_id"], "exchange": future["exchange"]}
    quotes = {}
    for symbol, identity in instruments.items():
        session_hours(datetime.now(timezone.utc), session)
        request = dict(identity, market_data_names=["last", "top_status", "bid_ask", "prior_close"])
        path = output / (symbol + ".json")
        subprocess.run(
            [
                "bash",
                str(broker_dir / "connect_ibkr.sh"),
                "snapshot",
                json.dumps(request),
                str(path),
            ],
            check=True,
            timeout=1010,
        )
        quotes[symbol] = normalize(
            read_json(path), request, path.with_suffix(".retrieved_at.txt").read_text().strip()
        )
    market = dict(
        source="IBKR MCP · last trade or index observation",
        contract=contract,
        future=future,
        captured_at=datetime.now(timezone.utc).isoformat(),
        quotes=quotes,
        synthetic=False,
        timestamp_basis="last.ts interpreted as Unix seconds. A receipt time or last-trade time cannot date a bid/ask.",
        executable_quote=False,
    )
    save(output / "market.json", market)
    return market
