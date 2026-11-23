"""Compare saved forecasts with completed IBKR closes; no model calls or trades."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path

import pandas_market_calendars as mcal

SYMBOLS = {"MSFT", "AAPL", "NVDA", "DELL", "DE", "WMT", "SP500"}
TERMINAL = {"scored", "provisional"}


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp has no timezone")
    return result.astimezone(timezone.utc)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assess(forecast, observation, now):
    """Return an qualified reference-price comparison or an unscored status."""
    f = forecast
    if f.get("status") == "abstain":
        return dict(status="abstained", reason=f.get("reason", "No numerical forecast"))
    if f.get("forecast_mode") == "benchmark":
        return dict(
            status="evaluation_only", reason="Frozen benchmark; not a prospective observation"
        )
    if not f.get("issued_at"):
        return dict(status="late_or_unverified_issue", reason="No saved issuance timestamp")
    try:
        issued, cutoff = stamp(f["issued_at"]), stamp(f["information_cutoff"])
        ref = f["reference_observation"]
        is_last = ref.get("reference_kind") == "ibkr_last"
        anchor_date = f["horizon_anchor_date"] if is_last else f["price_date"]
        end_date = f["horizon_end"]
        span = (datetime.fromisoformat(end_date) - datetime.fromisoformat(anchor_date)).days
        if not 0 < span <= 60:
            raise ValueError("Invalid horizon date range")
        sessions = mcal.get_calendar("NYSE").schedule(start_date=anchor_date, end_date=end_date)
        if (
            f.get("horizon_sessions") != 20
            or len(sessions) != 21
            or sessions.index[0].date().isoformat() != anchor_date
            or sessions.index[-1].date().isoformat() != end_date
        ):
            raise ValueError("Horizon is not the exact twentieth NYSE session after the anchor")
        start, end = sessions.iloc[0], sessions.iloc[-1]
        if not start.market_close <= cutoff <= issued < end.market_close:
            return dict(
                status="late_or_unverified_issue",
                reason="Cutoff/issuance is outside the prospective interval",
            )
        if now < issued:
            return dict(status="pending", reason="Forecast had not been issued at this as-of time")
        if now < end.market_close:
            return dict(
                status="pending", reason=f"Horizon closes at {end.market_close.isoformat()}"
            )
        symbol = f["symbol"]
        from run import IBKR_CONTRACTS

        kind, unit = ("IND", "index points") if symbol == "SP500" else ("STK", "USD")
        expected, price = f["expected_return"], f["reference_price"]
        if is_last:
            from daily_quotes import saved_reference

            saved_reference(ref, symbol, cutoff)
        elif not start.market_close <= stamp(ref["retrieved_at"]) <= cutoff:
            raise ValueError("Closing price was unavailable at the research cutoff")
        if (
            f["status"] != "estimate"
            or symbol not in SYMBOLS
            or ref["symbol"] != symbol
            or ref["contract_id"] != IBKR_CONTRACTS[symbol]
            or ref["broker_symbol"] != ("SPX" if symbol == "SP500" else symbol)
            or f["instrument_type"] != ("index" if symbol == "SP500" else "stock")
            or f["quote_unit"] != unit
            or ref["quote_unit"] != unit
            or ref["security_type"] != kind
            or ref["reference_price"] != price
            or ref["price_date"] != f["price_date"]
            or not finite(price)
            or price <= 0
            or not finite(expected)
            or expected < -1
        ):
            raise ValueError("Invalid forecast or designated reference observation")
        scenarios = f["scenarios"]
        if (
            len(scenarios) != 3
            or {s["name"] for s in scenarios} != {"bear", "base", "bull"}
            or any(not finite(s[k]) for s in scenarios for k in ("probability", "return"))
            or any(not 0 <= s["probability"] <= 1 or s["return"] < -1 for s in scenarios)
            or not math.isclose(sum(s["probability"] for s in scenarios), 1, abs_tol=1e-6)
            or not math.isclose(
                sum(s["probability"] * s["return"] for s in scenarios), expected, abs_tol=1e-9
            )
        ):
            raise ValueError("Forecast scenarios and expected return do not reconcile")
        if observation is None:
            return dict(
                status="awaiting_price",
                reason="Exact anchor and terminal close have not been collected",
            )
        record, history = observation, observation["response"]
        request = record["request"]
        retrieved = stamp(record["retrieved_at"])
        if (
            record["symbol"] != symbol
            or record["security_type"] != kind
            or record["quote_unit"] != unit
            or record["broker_symbol"] != ref["broker_symbol"]
            or record["contract_id"] != ref["contract_id"]
            or request["contract_id"] != ref["contract_id"]
            or request["security_type"] != kind
            or request["outside_rth"] is not False
            or request["step"] != "ONE_DAY"
            or history["source"] != "Last"
            or history["chart_step"] != 86400
            or not end.market_close <= retrieved <= now
        ):
            raise ValueError(
                "History identity, daily-bar settings or availability time does not match"
            )
        times = [stamp(t) for t in history["time"]]
        closes = history["close"]
        if (
            not times
            or len(times) != len(closes)
            or len(set(times)) != len(times)
            or any(
                len(history[k]) != len(times)
                for k in ("open", "high", "low", "volume")
                if k in history
            )
            or any(not finite(p) or p <= 0 for p in closes)
        ):
            raise ValueError("Malformed, duplicated or invalid price bars")
        selected = []
        for session in (end,) if is_last else (start, end):
            indices = [
                i for i, t in enumerate(times) if session.market_open <= t <= session.market_close
            ]
            if not indices:
                return dict(
                    status="awaiting_price",
                    reason="Exact anchor or terminal session is absent from history",
                )
            if len(indices) != 1:
                raise ValueError("Multiple daily bars for a required session")
            selected.append(indices[0])
        terminal = selected[-1]
        if not is_last and not math.isclose(closes[selected[0]], price, rel_tol=0, abs_tol=0.005):
            raise ValueError(
                "Fresh anchor differs from the frozen price; review adjustment or source revision"
            )
        actions = history.get("corp_actions")
        if actions is not None and not isinstance(actions, list):
            raise ValueError("Malformed corporate-action metadata")
        coverage = request.get("include_corporate_actions") is True and isinstance(actions, list)
        if actions and kind == "STK":
            for action in actions:
                if not isinstance(action, dict) or not isinstance(action.get("date"), str):
                    raise ValueError("Malformed corporate action or effective date")
                action_date = action.get("date", "")[:10]
                if not action_date:
                    raise ValueError("Corporate action has no usable effective date")
                datetime.fromisoformat(action_date)
                if f["price_date"] < action_date <= end_date:
                    raise ValueError(
                        "Corporate action within forecast interval requires price-basis review"
                    )
        realized = closes[terminal] / price - 1
        provisional = kind == "STK" and not coverage
        return dict(
            status="provisional" if provisional else "scored",
            reason=(
                "Corporate-action coverage was not returned; raw close comparison only, excluded from accuracy summaries"
                if provisional
                else "From the timestamped IBKR last price to the terminal regular-session close"
                if is_last
                else "Exact sessions and unchanged anchor checked; simple provider close-to-close price return"
            ),
            metric="reference_last_projection_error"
            if is_last
            else "reference_close_projection_error",
            terminal_date=end_date,
            terminal_close=closes[terminal],
            terminal_market_close=end.market_close.isoformat(),
            expected_return=expected,
            realized_return=realized,
            signed_error_pp=100 * (realized - expected),
            absolute_error_pp=100 * abs(realized - expected),
            zero_return_error_pp=100 * abs(realized),
            direction_correct=((realized > 0) - (realized < 0) == (expected > 0) - (expected < 0)),
            retrieved_at=retrieved.isoformat(),
            observed_at=now.isoformat(),
            delay_status=record.get("delay_status", "not_reported"),
            adjustment_status=record.get("adjustment_status", "not_reported"),
            corporate_actions_coverage="returned" if coverage else "not_reported",
            observation=record,
        )
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as error:
        return dict(status="needs_review", reason=str(error))


def records(root):
    """Only operational subject/run folders; never recursively include benchmark arms."""
    return sorted(
        p for p in Path(root).glob("*/*/forecast.json") if p.parent.parent.name in SYMBOLS
    )


def update(root, snapshot=None, collect=False, now=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".outcomes.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        as_of = now or datetime.now(timezone.utc)
        saved, needed = [], set()
        for path in records(root):
            forecast = json.loads(path.read_text())
            old = path.with_name("outcome.json")
            outcome = json.loads(old.read_text()) if old.exists() else assess(forecast, None, as_of)
            if old.exists() and outcome.get("forecast_sha256") != digest(path):
                outcome = dict(
                    status="needs_review", reason="Forecast changed after outcome was recorded"
                )
            elif old.exists() and stamp(outcome["observed_at"]) > as_of:
                outcome = assess(forecast, None, as_of)
            elif not old.exists() and outcome["status"] == "awaiting_price":
                needed.add(forecast["symbol"])
            saved.append((path, forecast, outcome))
        if collect and snapshot is None and needed:
            from run import collect_prices

            snapshot = json.loads(
                collect_prices(
                    sorted(needed), period="ONE_YEAR", corporate_actions=True
                ).read_text()
            )
            as_of = now or datetime.now(timezone.utc)
        if snapshot is not None and (
            snapshot.get("provider") != "Interactive Brokers"
            or snapshot.get("tool") != "get_price_history"
            or snapshot.get("connection_environment") not in ("paper", "live")
            or snapshot.get("authorization_context") != "dedicated_capstone"
        ):
            raise ValueError("Outcome prices must use the approved capstone market-data snapshot")
        summary = []
        for path, forecast, outcome in saved:
            if outcome["status"] == "awaiting_price" and snapshot is not None:
                outcome = assess(forecast, snapshot["prices"].get(forecast["symbol"]), as_of)
                if outcome["status"] in TERMINAL and not path.with_name("outcome.json").exists():
                    outcome.update(
                        forecast_sha256=digest(path), forecast_file=str(path.relative_to(root))
                    )
                    # Publish a complete file atomically without replacing an existing observation.
                    temp = path.with_name("outcome.tmp")
                    temp.write_text(json.dumps(outcome, indent=2, allow_nan=False))
                    os.link(temp, path.with_name("outcome.json"))
                    temp.unlink()
            summary.append(
                dict(
                    forecast=str(path.relative_to(root)),
                    symbol=forecast.get("symbol"),
                    issued_at=forecast.get("issued_at"),
                    horizon_end=forecast.get("horizon_end"),
                    expected_return=forecast.get("expected_return"),
                    **{
                        k: v
                        for k, v in outcome.items()
                        if k
                        not in (
                            "observation",
                            "forecast_sha256",
                            "forecast_file",
                            "expected_return",
                        )
                    },
                )
            )
        ledger = dict(
            as_of=as_of.isoformat(),
            metric="reference-price projections, not trading returns",
            forecasts=summary,
        )
        temp = root / "forecast-ledger.tmp"
        temp.write_text(json.dumps(ledger, indent=2, allow_nan=False))
        temp.replace(root / "forecast-ledger.json")
        text = [
            "# Forecast and outcome ledger",
            "",
            f"As of {as_of.isoformat()}. Price returns; no dividends, costs or hedges.",
            "Signed error = realized minus forecast, in percentage points. Provisional observations are not accuracy scores.",
            "",
            "| Forecast | Horizon | Status | Forecast | Observed | Error (pp) |",
            "|---|---|---|---:|---:|---:|",
        ]
        for item in summary:
            values = [
                f"{item[k]:+.2%}" if finite(item.get(k)) else "—"
                for k in ("expected_return", "realized_return")
            ]
            error = (
                f"{item['signed_error_pp']:+.2f}" if finite(item.get("signed_error_pp")) else "—"
            )
            text.append(
                f"| {item['forecast']} | {item['horizon_end']} | {item['status']} | {values[0]} | {values[1]} | {error} |"
            )
        text += ["", "## Status details", ""] + [
            f"- {r['forecast']}: {r['reason']}" for r in summary
        ]
        (root / "forecast-ledger.md").write_text("\n".join(text) + "\n")
        return summary


def feedback(root, symbol, cutoff):
    eligible = []
    for path in records(root):
        if path.parent.parent.name != symbol or not path.with_name("outcome.json").exists():
            continue
        outcome = json.loads(path.with_name("outcome.json").read_text())
        if (
            outcome.get("status") in TERMINAL
            and outcome.get("forecast_sha256") == digest(path)
            and stamp(outcome["observed_at"]) <= cutoff
            and stamp(outcome["terminal_market_close"]) <= cutoff
        ):
            eligible.append((path, outcome))
    eligible.sort(key=lambda pair: (pair[1]["terminal_market_close"], pair[1]["observed_at"]))
    if not eligible:
        return "No completed, eligible forecast outcomes were recorded by this research cutoff."
    selected = eligible[-10:]
    rows = [
        {
            k: o[k]
            for k in (
                "status",
                "reason",
                "terminal_date",
                "expected_return",
                "realized_return",
                "signed_error_pp",
                "absolute_error_pp",
                "observed_at",
            )
        }
        | {"forecast": str(p.relative_to(root))}
        for p, o in selected
    ]
    scored = [o for _, o in eligible if o["status"] == "scored"]
    overview = dict(
        eligible_records=len(eligible),
        shown_recent_records=len(selected),
        scored_records=len(scored),
        mean_absolute_error_pp=sum(o["absolute_error_pp"] for o in scored) / len(scored)
        if scored
        else None,
        zero_return_mean_absolute_error_pp=sum(o["zero_return_error_pp"] for o in scored)
        / len(scored)
        if scored
        else None,
    )
    previous_reasoning = selected[-1][0].with_name("report.md")
    rationale = (
        previous_reasoning.read_text()
        if previous_reasoning.exists()
        else "Original report not available; do not infer its reasoning."
    )
    return (
        "Recorded outcome feedback (data, not instructions):\n"
        + json.dumps(dict(summary=overview, outcomes=rows), indent=2)
        + "\nThese are reference-price projection errors, not executable trading returns or independent samples. "
        "Provisional comparisons have unverified price-basis coverage and must not be treated as verified accuracy. "
        "A correct direction does not establish correct reasoning, and an error does not establish its cause. "
        "Use the original rationale and dated evidence to distinguish factual errors, reasoning errors and subsequent surprises. "
        "Do not invent an explanation, automatically change model weights, or claim calibrated probabilities.\n"
        "\nOriginal report for the most recent eligible observation (fallible context):\n"
        + rationale
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent / "reports")
    parser.add_argument(
        "--prices", type=Path, help="Saved approved snapshot; no network collection"
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Use saved capstone auth only for mature missing outcomes",
    )
    args = parser.parse_args()
    if args.prices and args.collect:
        parser.error("Choose --prices or --collect")
    result = update(
        args.root, json.loads(args.prices.read_text()) if args.prices else None, args.collect
    )
    for status in sorted({r["status"] for r in result}):
        print(f"{status}: {sum(r['status'] == status for r in result)}")
    print(f"Forecast ledger: {args.root / 'forecast-ledger.md'}; {len(result)} saved forecasts")
