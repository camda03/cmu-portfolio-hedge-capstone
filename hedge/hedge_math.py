"""Compare signed MES target positions using a saved ledger statement."""

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from ledger import MES, MULTIPLIER, number, usd


# Format a dollar result to cents and display rounded negative zero as zero.
def _dollars(amount):
    rendered = usd(amount)
    return "0.00" if rendered == "-0.00" else rendered


# Compare candidate futures positions by exposure, fees, beta, and scenario profit or loss.
def evaluate_hedges(
    book,
    contract,
    futures_price,
    targets,
    cost_per_contract_usd,
    scenarios,
    betas=None,
    target_beta=None,
):
    """Returns and betas are decimals. Costs apply to the change in contracts."""
    price, cost = number(futures_price), number(cost_per_contract_usd)
    if not MES.fullmatch(contract) or price <= 0 or cost < 0:
        raise ValueError("Supply a quarterly MES contract, positive price and nonnegative cost.")
    if not isinstance(targets, list) or any(type(n) is not int for n in targets):
        raise ValueError("Targets must be a list of signed whole contract counts.")
    if not isinstance(scenarios, dict) or not scenarios:
        raise ValueError("Supply named scenarios.")

    leg = book["futures"].get(contract)
    current = leg["contracts"] if leg else 0
    nav = number(book["portfolio_value_usd"])
    if current:
        nav += current * MULTIPLIER * (price - number(leg["mark"]))
    if nav <= 0:
        raise ValueError("Portfolio NAV must be positive.")
    unit = MULTIPLIER * price
    other = sum(
        pos["contracts"] * MULTIPLIER * number(pos["mark"])
        for name, pos in book["futures"].items()
        if name != contract
    )
    stocks = {symbol: number(pos["value_usd"]) for symbol, pos in book["equities"].items()}
    betas = {s: number(b) for s, b in (betas or {}).items() if b is not None}
    if betas.keys() - stocks.keys():
        raise ValueError("A beta was supplied for an unknown holding.")
    missing = sorted(stocks.keys() - betas.keys())
    equity_beta_dollars = None if missing else sum(stocks[s] * betas[s] for s in stocks)

    moves = {}
    for name, case in scenarios.items():
        index = number(case["index_return"])
        overrides = case.get("stock_returns", {})
        if index < -1 or overrides.keys() - stocks.keys():
            raise ValueError(f"Invalid index return or unknown holding in {name}.")
        rates = {s: betas[s] * index for s in betas}
        rates.update({s: number(r) for s, r in overrides.items()})
        if any(r < -1 for r in rates.values()):
            raise ValueError(f"Stock return below -100% in {name}. Supply a valid stock scenario.")
        equity_pnl = (
            sum(stocks[s] * r for s, r in rates.items()) if rates.keys() == stocks.keys() else None
        )
        moves[name] = index, equity_pnl

    rows = []
    for target in dict.fromkeys([current, *targets]):
        delta = target - current
        # Trade only the difference from the open position. A hold incurs no new fee.
        fees = abs(delta) * cost
        exposure = other + target * unit
        net_beta = None if missing else (equity_beta_dollars + exposure) / nav
        results = {}
        for name, (index, equity_pnl) in moves.items():
            overlay = exposure * index - fees  # A short gains when the index falls.
            results[name] = dict(
                overlay_after_cost_usd=_dollars(overlay),
                versus_keep_usd=_dollars(delta * unit * index - fees),
                total_pnl_usd=None if equity_pnl is None else _dollars(equity_pnl + overlay),
            )
        rows.append(
            dict(
                target_contracts=target,
                change_contracts=delta,
                change_cost_usd=usd(fees),
                net_beta=None if net_beta is None else float(net_beta),
                scenarios=results,
            )
        )
    ideal = None
    if target_beta is not None and not missing:
        ideal = float((number(target_beta) * nav - equity_beta_dollars - other) / unit)
    return dict(
        contract=contract,
        current_contracts=current,
        mes_unit_notional_usd=usd(unit),
        valuation_nav_usd=usd(nav),
        missing_betas=missing,
        fractional_target_for_beta=ideal,
        candidates=rows,
    )


CALCULATOR = {
    "type": "function",
    "name": "calculate_portfolio_hedge",
    "strict": True,
    "description": "Compare permitted signed MES targets on the fixed decision-time portfolio. Supply one custom index move and optional individual-stock returns as decimals. Results include the policy stresses, this what-if, signed exposure, fees and beta. A target beta also returns a theoretical fractional position.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "targets": {"type": "array", "items": {"type": "integer"}},
            "index_return": {
                "type": "number",
                "description": "Decimal S&P 500 return, for example -0.015.",
            },
            "stock_returns": {
                "type": "array",
                "description": "Optional stock-specific overrides for this what-if. Use [] for benchmark-beta estimates.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"symbol": {"type": "string"}, "return": {"type": "number"}},
                    "required": ["symbol", "return"],
                },
            },
            "target_beta": {"type": ["number", "null"]},
        },
        "required": ["targets", "index_return", "stock_returns", "target_beta"],
    },
}


# Validate a model tool request and evaluate its hedge targets against the recorded market.
def calculator_call(inputs, arguments):
    args = json.loads(arguments)
    if not isinstance(args, dict) or set(args) != set(CALCULATOR["parameters"]["required"]):
        raise ValueError("Supply targets, index_return, stock_returns and target_beta.")
    market, table, policy = inputs["market"], inputs["alternatives"], inputs["policy"]
    permitted = {r["target_contracts"] for r in table["candidates"] if r["eligible_target"]} | {
        table["current_contracts"]
    }
    targets, overrides = args["targets"], args["stock_returns"]
    if (
        not isinstance(targets, list)
        or not targets
        or any(type(n) is not int or n not in permitted for n in targets)
    ):
        raise ValueError(
            "Choose signed whole targets from the decision's permitted positions or its current position."
        )
    if not isinstance(overrides, list) or any(
        not isinstance(r, dict) or set(r) != {"symbol", "return"} for r in overrides
    ):
        raise ValueError("Each stock override needs a held symbol and a decimal return.")
    stocks = {r["symbol"]: r["return"] for r in overrides}
    if len(stocks) != len(overrides):
        raise ValueError("A stock may have only one override.")
    scenario = {"index_return": args["index_return"], "stock_returns": stocks}
    return evaluate_hedges(
        inputs["portfolio"],
        market["contract"],
        market["quotes"][market["contract"]]["last"],
        targets,
        policy["cost_per_contract_usd"],
        policy["scenarios"] | {"what_if": scenario},
        policy.get("betas"),
        args["target_beta"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book", type=Path, help="ledger status JSON or paired research JSON")
    parser.add_argument("assumptions", type=Path, help="scenario and valuation inputs in JSON")
    args = parser.parse_args()
    try:
        saved = json.loads(args.book.read_bytes(), parse_float=Decimal)
        inputs = json.loads(args.assumptions.read_bytes(), parse_float=Decimal)
        print(
            json.dumps(
                evaluate_hedges(saved.get("portfolio", saved), **inputs), indent=2, allow_nan=False
            )
        )
    except (OSError, ValueError, InvalidOperation, KeyError, TypeError, AttributeError) as exc:
        parser.exit(1, f"Hedge calculation: {exc}\n")
