"""Frozen-reference price warnings and deterministic replay; no network or model calls."""

import argparse
import copy
from datetime import datetime
import hashlib
from html import escape
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
DEFAULT_POLICY = dict(
    cumulative_sigma=3.0,
    recent_sigma=3.5,
    extreme_sigma=6.0,
    min_sigma=0.002,
    time_floor_sessions=1 / 26,
    max_age_seconds=120.0,
    max_spread_bps=100.0,
    price_tolerance_bps=50.0,
    confirmation_seconds=60.0,
)


# Encode a price-watch record in a stable JSON representation.
def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


# Fingerprint the canonical record to identify the exact watch or state.
def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


# Parse a timestamp and require an explicit time-zone offset.
def instant(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("A timezone is required.")
    return stamp


# Check that a proposed price is a finite number greater than zero.
def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


# Build the price watch from a forecast, comparable daily closes, and its trigger policy.
def make_watch(forecast, history, policy=None):
    """Require comparable daily closes with explicit no-action coverage for this raw-basis pilot."""
    f, p = copy.deepcopy(forecast), dict(DEFAULT_POLICY, **(policy or {}))
    if set(p) != set(DEFAULT_POLICY) or any(not positive(v) for v in p.values()):
        raise ValueError("Invalid price policy.")
    if p["extreme_sigma"] < max(p["cumulative_sigma"], p["recent_sigma"]):
        raise ValueError("Extreme threshold must exceed ordinary limits.")
    if any(
        not isinstance(f[k], str) or not f[k] for k in ("id", "symbol", "currency", "price_basis")
    ):
        raise ValueError("Forecast identity is incomplete.")
    ref, issued, end = map(instant, (f["reference_at"], f["issued_at"], f["valid_until"]))
    if (
        not ref <= issued < end
        or not positive(f["reference_price"])
        or not positive(f["target_price"])
    ):
        raise ValueError("Invalid forecast anchor or target.")
    if f["horizon_sessions"] != 20 or len(history) < 61:
        raise ValueError("Require a 20-session horizon and at least 61 completed daily closes.")
    times = [instant(h["time"]) for h in history]
    if times != sorted(set(times)) or times[-1] != ref or any(t > ref for t in times):
        raise ValueError("History must be unique, ordered and end at the frozen reference.")
    if any(not positive(h["close"]) or h.get("corporate_action") is not False for h in history):
        raise ValueError("Invalid or corporate-action-ambiguous history.")
    if not math.isclose(history[-1]["close"], f["reference_price"], rel_tol=0, abs_tol=0.005):
        raise ValueError("History and reference price disagree.")
    returns = [math.log(b["close"] / a["close"]) for a, b in zip(history[-61:-1], history[-60:])]
    watch = dict(
        forecast=f,
        policy=p,
        daily_sigma=max(p["min_sigma"], statistics.stdev(returns)),
        history_sha256=digest(history),
        history_returns=60,
        method="Prior-only daily log-return scale; heuristic limits, not probability bands.",
    )
    return dict(watch, sha256=digest(watch))


# Explain why a broker observation is unsuitable for evaluating this watch.
def observation_problem(watch, o):
    f, p = watch["forecast"], watch["policy"]
    try:
        if any(o[k] != f[k] for k in ("symbol", "currency", "price_basis")):
            return "Instrument, currency or price basis mismatch."
        if o.get("corporate_action") is not False or o.get("halted") is not False:
            return "Corporate-action coverage or halt status is unsuitable."
        if o.get("is_close", False) or o.get("data_mode") not in ("REALTIME", "REPLAY"):
            return "Price is a prior close, frozen, delayed or of unknown availability."
        observed, received, checked = map(
            instant, (o["observed_at"], o["received_at"], o["checked_at"])
        )
        if not instant(f["issued_at"]) <= observed <= received <= checked:
            return "Observation chronology is unsuitable."
        if (checked - observed).total_seconds() > p["max_age_seconds"]:
            return "Price observation is stale."
        t = o["elapsed_sessions"]
        if (
            not positive(o["price"])
            or type(t) not in (int, float)
            or not math.isfinite(t)
            or not 0 <= t <= 20
        ):
            return "Invalid price or elapsed session count."
        if ("bid" in o) != ("ask" in o):
            return "Incomplete bid/ask corroboration."
        if "bid" in o:
            bid, ask = o["bid"], o["ask"]
            if not positive(bid) or not positive(ask) or bid > ask:
                return "Invalid bid/ask."
            mid = (bid + ask) / 2
            if (ask - bid) / mid * 10000 > p["max_spread_bps"]:
                return "Bid/ask spread exceeds the observation-quality limit."
            if (
                not bid * (1 - p["price_tolerance_bps"] / 10000)
                <= o["price"]
                <= ask * (1 + p["price_tolerance_bps"] / 10000)
            ):
                return "Last price is not corroborated by the supplied bid/ask."
            if (
                not 0
                <= (checked - instant(o["bid_ask_at"])).total_seconds()
                <= p["max_age_seconds"]
            ):
                return "Bid/ask corroboration has an invalid observation time."
    except (KeyError, ValueError, TypeError, OverflowError):
        return "Required observation metadata is missing or invalid."
    return ""


# Evaluate one price observation and return the proposed action and updated watch state.
def evaluate(watch, observation, state=None, news=None):
    """One observation -> inspectable shadow action plus JSON-persistable next state."""
    if watch["sha256"] != digest({k: v for k, v in watch.items() if k != "sha256"}):
        raise ValueError("Frozen watch was modified.")
    f, p, o = watch["forecast"], watch["policy"], observation
    state = copy.deepcopy(state or {})
    if state.get("forecast_id") == f["id"] and state.get("watch_sha256") != watch["sha256"]:
        raise ValueError("Same forecast ID cannot change its frozen monitoring policy.")
    if state.get("watch_sha256") != watch["sha256"]:
        state = dict(
            forecast_id=f["id"],
            watch_sha256=watch["sha256"],
            previous=dict(
                price=f["reference_price"], observed_at=f["reference_at"], elapsed_sessions=0
            ),
            candidates={},
            alerted=[],
            analysis_pending=False,
        )
    news = news or dict(decision="quiet", event_ids=[])
    if news["decision"] not in ("quiet", "verify", "reassess", "coverage_gap") or not isinstance(
        news["event_ids"], list
    ):
        raise ValueError("News must be a validated monitor decision with event IDs.")
    if news["decision"] in ("reassess", "verify") and not news["event_ids"]:
        raise ValueError("News escalation requires evidence IDs.")
    result = dict(
        action="keep_forecast",
        execute=False,
        dispatch_requested=False,
        forecast_id=f["id"],
        price_status="unchecked",
        reasons=[],
        signals=[],
        state=state,
    )
    signals = (
        ["news:" + str(i) for i in news["event_ids"]] if news["decision"] == "reassess" else []
    )
    try:
        expired = instant(o["checked_at"]) > instant(f["valid_until"])
    except (KeyError, ValueError, TypeError):
        expired = False
    problem = observation_problem(watch, o)
    if expired:
        result.update(action="expired", price_status="expired")
        signals = []  # A new forecast is needed, not reassessment of an expired target.
    elif o.get("market_session") in ("CLOSED", "EXTENDED"):
        result.update(action="market_closed", price_status="outside_regular_session")
        state["candidates"] = {}  # Keep the last valid close for the opening-gap check.
    elif problem or o.get("market_session") != "REGULAR":
        result.update(
            action="restore_coverage",
            price_status="data_gap",
            reasons=[problem or "Unknown market session."],
        )
        state["previous"], state["candidates"] = None, {}
    else:
        prior = state["previous"]
        # The calendar clock is unchanged from a close to the next exact open.
        if prior and (
            instant(o["observed_at"]) <= instant(prior["observed_at"])
            or o["elapsed_sessions"] < prior["elapsed_sessions"]
            or (
                o["elapsed_sessions"] == prior["elapsed_sessions"]
                and o.get("at_session_open") is not True
            )
        ):
            result.update(
                action="restore_coverage",
                price_status="duplicate_or_out_of_order",
                reasons=["A repeated or out-of-order price cannot confirm a move."],
            )
        else:
            t, price, sigma = o["elapsed_sessions"], o["price"], watch["daily_sigma"]
            drift = math.log(f["target_price"] / f["reference_price"]) / 20

            # Measure the price move in volatility units after accounting for elapsed sessions and drift.
            def score(base_price, base_t, q=price):
                dt = max(t - base_t, p["time_floor_sessions"])
                return (math.log(q / base_price) - drift * (t - base_t)) / (sigma * math.sqrt(dt))

            scores = {"cumulative": score(f["reference_price"], 0)}
            bases = {"cumulative": (f["reference_price"], 0)}
            if prior:
                pending = next(
                    (v for k, v in state["candidates"].items() if k.startswith("recent:")), None
                )
                base = (
                    (pending["base_price"], pending["base_t"])
                    if pending
                    else (prior["price"], prior["elapsed_sessions"])
                )
                scores["recent"] = score(*base)
                bases["recent"] = base
            result.update(
                price_status="valid",
                cumulative_z=scores["cumulative"],
                recent_z=scores.get("recent"),
                reference_price=f["reference_price"] * math.exp(drift * t),
            )
            candidates = state["candidates"]
            # A recent shock is confirmed against its original pre-shock anchor,
            # even when the next incremental return is flat.
            for key, candidate in list(candidates.items()):
                kind, direction = key.split(":")
                z = score(candidate["base_price"], candidate["base_t"])
                if z * (1 if direction == "up" else -1) < p[kind + "_sigma"]:
                    del candidates[key]
                elif (
                    instant(o["observed_at"]) - instant(candidate["first_at"])
                ).total_seconds() >= p["confirmation_seconds"]:
                    signals.append("price:" + key)
                    del candidates[key]
            for kind, z in scores.items():
                if abs(z) < p[kind + "_sigma"]:
                    continue
                key = kind + (":" + ("up" if z > 0 else "down"))
                if "price:" + key in state["alerted"] or "price:" + key in signals:
                    signals.append("price:" + key)
                    continue
                if key not in candidates:
                    candidates[key] = dict(
                        base_price=bases[kind][0], base_t=bases[kind][1], first_at=o["observed_at"]
                    )
                corroborated = False
                if abs(z) >= p["extreme_sigma"] and "bid" in o and o.get("bid_ask_at"):
                    quote_at = instant(o["bid_ask_at"])
                    age = (instant(o["checked_at"]) - quote_at).total_seconds()
                    mid_z = score(*bases[kind], q=(o["bid"] + o["ask"]) / 2)
                    corroborated = (
                        0 <= age <= p["max_age_seconds"]
                        and mid_z * z > 0
                        and abs(mid_z) >= p["extreme_sigma"]
                    )
                if corroborated:
                    signals.append("price:" + key)
                    result["extreme_corroborated"] = True
            state["previous"] = {k: o[k] for k in ("price", "observed_at", "elapsed_sessions")}
            if candidates and not signals:
                result.update(
                    action="verify_price",
                    reasons=[
                        "A threshold was crossed; await a distinct valid observation confirming the move."
                    ],
                )
    new_signals = sorted(set(signals) - set(state["alerted"]))
    result["signals"] = sorted(set(signals))
    if new_signals:
        result.update(
            action="update_pending_analysis" if state["analysis_pending"] else "full_analysis",
            dispatch_requested=not state["analysis_pending"],
        )
        state["alerted"] = sorted(set(state["alerted"]) | set(new_signals))
        state["analysis_pending"] = True
    elif signals:
        result["action"] = "already_flagged"
    elif (
        not expired
        and news["decision"] == "verify"
        and result["action"] in ("keep_forecast", "market_closed", "restore_coverage")
    ):
        result["action"] = "verify_evidence"
    elif not expired and news["decision"] == "coverage_gap" and result["action"] == "keep_forecast":
        result["action"] = "restore_coverage"
    return result


# Replay the saved observations through the price watch and compare expected actions.
def replay(suite):
    watch = make_watch(suite["forecast"], suite["history"], suite.get("policy"))
    rows = []
    for case in suite["cases"]:
        state, output = None, []
        for i, observation in enumerate(case["observations"]):
            result = evaluate(watch, observation, state, case.get("news", {}).get(str(i)))
            state = result["state"]
            output.append(result)
        actions = [r["action"] for r in output]
        rows.append(
            dict(
                id=case["id"],
                description=case["description"],
                expected=case["expected_actions"],
                actions=actions,
                passed=actions == case["expected_actions"],
                observations=output,
                dispatch_requests=sum(r["dispatch_requested"] for r in output),
            )
        )
    return dict(
        watch=watch,
        rows=rows,
        passed=sum(r["passed"] for r in rows),
        total=len(rows),
        note="Synthetic engineering replay; no market calibration, model calls or executed handoffs.",
    )


# Dispatch the price-watch command and write its requested results.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["replay"], nargs="?", default="replay")
    parser.add_argument("--input", type=Path, default=ROOT / "fixtures/prices/replay.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/price-tests")
    args = parser.parse_args()
    sources = {
        "replay.json": args.input.read_bytes(),
        "price_watch.py": Path(__file__).read_bytes(),
    }
    report = replay(json.loads(sources["replay.json"]))
    report["source_sha256"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in sources.items()
    }
    folder = args.output / digest(report["source_sha256"])[:20]
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in sources.items():
        (folder / name).write_bytes(data)
    text = json.dumps(report, indent=2, allow_nan=False)
    (folder / "summary.json").write_text(text)
    markdown = f"# Price warning replay: {report['passed']}/{report['total']}\n\n{report['note']}\n\n| Case | Actions | Match |\n|---|---|---|\n"
    markdown += "".join(
        f"| {r['id']} | {', '.join(r['actions'])} | {r['passed']} |\n" for r in report["rows"]
    )
    (folder / "summary.md").write_text(markdown)
    (args.output.parent / "price-current.txt").write_text(str(folder.resolve()) + "\n")
    rows = "".join(
        f"<tr><td>{escape(r['id'])}</td><td>{escape(r['description'])}</td><td>{escape(', '.join(r['actions']))}</td><td>{r['passed']}</td></tr>"
        for r in report["rows"]
    )
    (folder / "summary.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Price warning replay</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto}td,th{border:1px solid #ccc;padding:10px}table{border-collapse:collapse}pre{white-space:pre-wrap}</style>'
        + f"<h1>Price warning replay: {report['passed']}/{report['total']}</h1><p>{report['note']}</p><table><tr><th>Case</th><th>Purpose</th><th>Actions</th><th>Match</th></tr>{rows}</table><details><summary>Full records</summary><pre>{escape(text)}</pre></details>"
    )
    print(
        f"Price replay: {report['passed']}/{report['total']}; no model calls\nHTML: {folder}/summary.html"
    )
    if report["passed"] != report["total"]:
        raise SystemExit("Price replay mismatch; inspect before proceeding.")


if __name__ == "__main__":
    main()
