"""Read an IBKR last observation with its original exchange timestamp."""

from datetime import date, datetime, timezone
import json
import math
import subprocess
from zoneinfo import ZoneInfo

from monitor import read, stamp, write
from securities import SECURITIES

NEW_YORK = ZoneInfo("America/New_York")


def reference(snapshot, symbol, cutoff):
    """Use the timestamp in IBKR's last field, including outside the regular session."""
    item = snapshot["observations"][symbol]
    last = item["last"]
    instrument = SECURITIES[symbol]
    broker_symbol = "SPX" if symbol == "SP500" else symbol
    price = last["price"]
    recorded = datetime.fromisoformat(snapshot["recorded_at"].replace("Z", "+00:00"))
    retrieved_on = date.fromisoformat(snapshot["retrieved_on"])
    if (
        snapshot["schema"] != "ibkr-last-v1"
        or snapshot["provider"] != "Interactive Brokers"
        or snapshot["tool"] != "get_price_snapshot"
        or item["contract_id"] != instrument["contract_id"]
        or item["broker_symbol"] != broker_symbol
        or type(price) not in (int, float)
        or not math.isfinite(price)
        or price <= 0
        or type(last["ts"]) is not int
        or last["ts"] <= 0
        or recorded.tzinfo is None
        or recorded > cutoff
        or retrieved_on != recorded.astimezone(NEW_YORK).date()
    ):
        raise ValueError("Invalid IBKR last observation for " + symbol)
    observed = datetime.fromtimestamp(last["ts"], timezone.utc).astimezone(NEW_YORK)
    if observed > recorded or observed.date() > retrieved_on:
        raise ValueError("IBKR last timestamp follows its retrieval for " + symbol)
    return dict(
        reference_kind="ibkr_last",
        symbol=symbol,
        broker_symbol=broker_symbol,
        contract_id=item["contract_id"],
        security_type=instrument["security_type"],
        quote_unit=instrument["quote_unit"],
        reference_price=price,
        price_date=observed.date().isoformat(),
        last_trade_at=observed.isoformat(),
        last=last,
        retrieved_on=snapshot["retrieved_on"],
        recorded_at=snapshot["recorded_at"],
        price_source=f"Interactive Brokers get_price_snapshot last; {broker_symbol}; {observed.isoformat()}; retrieved {retrieved_on}",
    )


def saved_reference(observation, symbol, cutoff):
    packet = dict(
        schema="ibkr-last-v1",
        provider="Interactive Brokers",
        tool="get_price_snapshot",
        retrieved_on=observation["retrieved_on"],
        recorded_at=observation["recorded_at"],
        observations={symbol: observation},
    )
    checked = reference(packet, symbol, cutoff)
    if any(observation.get(key) != value for key, value in checked.items()):
        raise ValueError("Saved IBKR last price or timestamp differs from the original observation")
    return checked


def off_hours_snapshot(source_dir, folder, symbol):
    instrument = SECURITIES[symbol]
    request = dict(contract_id=instrument["contract_id"], market_data_names=["last"])
    if symbol == "SP500":
        request["exchange"] = "CBOE"
    path = folder / (symbol + "-snapshot.json")
    subprocess.run(
        [
            "timeout",
            "--kill-after=5s",
            "45s",
            "bash",
            str(source_dir / "connect_ibkr.sh"),
            "snapshot",
            json.dumps(request),
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    raw = read(path)
    if raw.get("isError"):
        raise ValueError("IBKR snapshot unavailable")
    data = raw.get("structuredContent") or json.loads(raw["content"][0]["text"])
    recorded = stamp()
    snapshot = dict(
        schema="ibkr-last-v1",
        provider="Interactive Brokers",
        tool="get_price_snapshot",
        retrieved_on=datetime.fromisoformat(recorded).astimezone(NEW_YORK).date().isoformat(),
        recorded_at=recorded,
        observations={
            symbol: dict(
                contract_id=instrument["contract_id"],
                broker_symbol="SPX" if symbol == "SP500" else symbol,
                last=data["last"],
            )
        },
    )
    observation = reference(snapshot, symbol, datetime.fromisoformat(recorded))
    target = folder / "last.json"
    write(target, snapshot)
    return target, observation["price_date"]
