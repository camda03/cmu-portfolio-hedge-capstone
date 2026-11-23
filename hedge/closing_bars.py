"""Capture IBKR regular-hours daily bars and select the last completed common session."""

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from hedge_session import ROOT, read_json, require, save
from ledger import EASTERN, MES, instant, number
from market_snapshot import broker_payload

# IBKR identities from the Big Seven producer's dashboard/securities.json.
CONIDS = {
    "AAPL": 265598,
    "DE": 6393,
    "DELL": 346218218,
    "MSFT": 272093,
    "NVDA": 4815747,
    "WMT": 13824,
    "SP500": 416904,
}


# Map the configured stocks, index, and futures month to their broker instruments.
def contracts(future):
    contract = "MES-" + future["contract_month"]
    require(
        MES.fullmatch(contract)
        and future["root"] == "MES"
        and future["exchange"] == "CME"
        and future["multiplier"] == 5
        and type(future["contract_id"]) is int
        and future["contract_id"] > 0
        and future["last_trading_date"].startswith(future["contract_month"]),
        "Invalid MES contract specification.",
    )
    instruments = {s: dict(contract_id=v, security_type="STK") for s, v in CONIDS.items()}
    instruments["SP500"].update(security_type="IND", exchange="CBOE")
    instruments[contract] = dict(
        contract_id=future["contract_id"], security_type="FUT", exchange="CME"
    )
    return contract, instruments


# Find sessions for which every required exchange had closed by the cutoff.
def completed_sessions(at):
    import pandas as pd
    import pandas_market_calendars as mcal

    end = at.astimezone(EASTERN).date()
    schedules = {
        name: mcal.get_calendar(name).schedule(end - timedelta(days=16), end)["market_close"]
        for name in ("NYSE", "CME_Equity")
    }
    closes = pd.DataFrame(schedules).dropna()
    return closes[closes.max(axis=1) <= at]


# Build a common closing-price snapshot from the broker bars for all required instruments.
def close_market(capture, future, now):
    contract, identities = contracts(future)
    rows = capture["instruments"]
    require(
        capture["schema"] == "ibkr-daily-close-capture-v1"
        and len(rows) == len(identities)
        and {r["symbol"] for r in rows} == set(identities),
        "Eight distinct IBKR instruments are required.",
    )
    times = [instant(r["retrieved_at"]) for r in rows]
    require(max(times) <= now, "Broker receipt postdates this rehearsal.")
    sessions = completed_sessions(max(times))
    require(len(sessions) >= 2, "Two completed common sessions are required.")
    previous, day = [v.date().isoformat() for v in sessions.index[-2:]]
    require(
        day.replace("-", "") < future["last_trading_date"], "The selected MES is at or past expiry."
    )
    quotes = {}
    for row in rows:
        symbol, request, bars = row["symbol"], row["request"], row["bar_series"]
        identity = identities[symbol]
        require(
            all(request.get(k) == v for k, v in identity.items())
            and request.get("exchange") == identity.get("exchange")
            and request["step"] == "ONE_DAY"
            and request["outside_rth"] is False
            and bars["chart_step"] == 86400
            and bars["source"] == "Last",
            f"Wrong IBKR identity or daily series for {symbol}.",
        )
        end = sessions.iloc[-1]["CME_Equity" if symbol == contract else "NYSE"].to_pydatetime()
        delay = number(bars.get("delayed", 0))
        require(
            delay >= 0 and (instant(row["retrieved_at"]) - end).total_seconds() >= delay,
            f"{symbol} was retrieved before the session and reported feed delay elapsed.",
        )
        labels = bars["time"]
        days = [instant(stamp).astimezone(EASTERN).date().isoformat() for stamp in labels]
        require(
            len(labels) == len(bars["close"]) and days == sorted(set(days)) and day in days,
            f"Missing or ambiguous {day} daily close for {symbol}.",
        )
        i = days.index(day)
        price = bars["close"][i]
        prior = bars["close"][days.index(previous)] if previous in days else None
        require(
            number(price) > 0 and (symbol != contract or number(price) % number(".25") == 0),
            f"Invalid closing price for {symbol}.",
        )
        require(prior is None or number(prior) > 0, f"Invalid previous close for {symbol}.")
        quotes[symbol] = dict(
            **identity,
            last=price,
            mode="HISTORICAL_CLOSE",
            is_close=True,
            observed_at=None,
            bar_label=labels[i],
            session_date=day,
            valuation_at=end.isoformat(),
            received_at=row["retrieved_at"],
            reported_delay_seconds=int(delay),
            prior_close=prior,
        )
    return dict(
        source="IBKR regular-hours daily Last bars",
        synthetic=False,
        price_mode="last_close_rehearsal",
        contract=contract,
        future=future,
        session_date=day,
        previous_session=previous,
        captured_at=max(times).isoformat(),
        quotes=quotes,
        executable_quote=False,
        timestamp_basis="IBKR labels each bar at its start. Valuation uses the venue session date; "
        "the last-trade time and bid/ask are unavailable. The MES bar is not a settlement feed.",
    )


# Request and save the broker's daily bars for each instrument in the policy.
def capture_bars(broker_dir, policy, out, step_count=5):
    _, instruments = contracts(policy["future"])
    rows = []
    with TemporaryDirectory() as folder:
        for symbol, identity in instruments.items():
            path = Path(folder) / (symbol + ".json")
            req = dict(**identity, step="ONE_DAY", step_count=step_count, outside_rth=False)
            subprocess.run(
                [
                    "bash",
                    str(broker_dir.resolve() / "connect_ibkr.sh"),
                    "history",
                    json.dumps(req),
                    str(path),
                ],
                check=True,
                timeout=1010,
            )
            rows.append(
                dict(
                    symbol=symbol,
                    request=req,
                    retrieved_at=path.with_suffix(".retrieved_at.txt").read_text().strip(),
                    bar_series=broker_payload(read_json(path)),
                )
            )
    capture = dict(schema="ibkr-daily-close-capture-v1", instruments=rows)
    market = close_market(capture, policy["future"], datetime.now(timezone.utc))
    save(out, capture)
    return market["session_date"]


# Estimate stock betas from the most recent 63 shared daily returns.
def calibrated_policy(capture, policy, session, mode, now):
    """Price-return betas on the most recent 63 common complete daily returns."""
    import pandas as pd

    price_day = close_market(capture, policy["future"], now)["session_date"]
    require(
        price_day == session if mode == "close" else price_day < session,
        "The independently captured closing session does not match the requested use.",
    )
    series = {}
    for row in capture["instruments"]:
        if row["symbol"] in CONIDS:
            bars = row["bar_series"]
            dates = [
                instant(value).astimezone(EASTERN).date().isoformat() for value in bars["time"]
            ]
            series[row["symbol"]] = pd.Series(bars["close"], index=dates, dtype=float)
    prices = pd.DataFrame(series).sort_index().loc[:price_day].tail(64)  # 64 closes yield 63 daily returns.
    require(
        len(prices) == 64 and prices.notna().all().all() and (prices > 0).all().all(),
        "Need 64 aligned positive IBKR daily closes for every equity and the cash index.",
    )
    returns = prices.pct_change(fill_method=None).iloc[1:]
    variance = returns["SP500"].var()
    require(variance > 0, "The S&P 500 return variance is zero.")
    betas = returns.drop(columns="SP500").apply(lambda r: r.cov(returns["SP500"]) / variance)
    return policy | {
        "betas": betas.round(8).to_dict(),
        "beta_source": f"Independent IBKR daily closes {prices.index[0]} through {price_day}, 63 common price returns, OLS with intercept.",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--broker-dir", type=Path, required=True)
    p.add_argument("--policy", type=Path, default=ROOT / "demo_assets/pilot.example.json")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--policy-out", type=Path, help="write the policy with six independently estimated betas"
    )
    p.add_argument("--for-mode", choices=["close", "live"])
    p.add_argument("--session", help="intended price or live trading session")
    a = p.parse_args()
    policy = read_json(a.policy)
    if a.policy_out:
        require(a.for_mode and a.session, "Supply --for-mode and --session with --policy-out.")
        capture_bars(a.broker_dir, policy, a.out, step_count=70)
        save(
            a.policy_out,
            calibrated_policy(
                read_json(a.out), policy, a.session, a.for_mode, datetime.now(timezone.utc)
            ),
        )
        print(a.policy_out)
    else:
        print(capture_bars(a.broker_dir, policy, a.out), a.out)
