"""Controlled two-outcome monitor test; supplied evidence, shadow referrals only."""

import argparse
import fcntl
from html import escape
from importlib.metadata import version
import os
from pathlib import Path
from time import monotonic, sleep
from typing import Literal

from pydantic import BaseModel, ConfigDict
from monitor import digest, dump, has_gap, read, stamp, token_cost, write
import price_watch as prices

ROOT = Path(__file__).resolve().parent
MODELS = ("gpt-6.1-sol", "gpt-6-luna")
SETTINGS = dict(
    reasoning={"effort": "max"},
    max_output_tokens=128000,
    service_tier="default",
    background=True,
    store=False,
    tools=[],
)
# Standard short-context USD/M: input, cache read, 30m write, 12h write, output.
# Checked 2026-11-12 UTC: https://developers.openai.com/api/docs/pricing
RATES = {
    MODELS[0]: (2.0, 0.10, 2.50, 5.0, 10.0),
    MODELS[1]: (0.10, 0.01, 0.125, 0.25, 0.50),
    "gpt-6-astra": (10.0, 1.0, 12.5, 25.0, 50.0),
}
FILES = (
    "monitor_scope.py",
    "monitor_scope.sh",
    "monitor_scope_prompt.md",
    "monitor.py",
    "price_watch.py",
    "prompts.md",
    "fixtures/prices/replay.json",
    "fixtures/monitor-scope/watch.json",
    "fixtures/monitor-scope/cases.json",
    "fixtures/monitor-scope/labels.json",
)
CASE_FIELDS = (
    "id",
    "evidence_mode",
    "since",
    "as_of",
    "coverage",
    "seen_event_ids",
    "prior_events",
    "events",
)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    case_id: str
    decision: Literal["continue", "reassess"]
    condition_ids: list[str]
    evidence_ids: list[str]
    reason: str
    watch_for: str
    review_question: str


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decisions: list[Decision]


# Load the watch, evidence cases, and expected outcomes for the scope evaluation.
def inputs(root):
    return tuple(
        read(root / "fixtures/monitor-scope" / (name + ".json"))
        for name in ("watch", "cases", "labels")
    )


# Check that the scope cases and labels cover the expected watch conditions.
def validate_inputs(watch, cases, labels):
    ids = [c["id"] for c in cases]
    conditions = [c["id"] for c in watch["conditions"]]
    if (
        watch["schema"] != "monitor-scope-watch-v1"
        or watch["synthetic"] is not True
        or not ids
        or len(set(ids)) != len(ids)
        or set(ids) != set(labels)
        or not conditions
        or len(set(conditions)) != len(conditions)
    ):
        raise ValueError("Require a synthetic watch, unique cases/conditions and separate labels.")
    for case in cases:
        times = [
            prices.instant(t)
            for t in (watch["issued_at"], case["since"], case["as_of"], watch["valid_until"])
        ]
        if times != sorted(times) or case["evidence_mode"] != "synthetic":
            raise ValueError("Case lies outside the synthetic forecast horizon.")
        events = case["prior_events"] + case["events"]
        event_ids = [e["id"] for e in events]
        if len(set(event_ids)) != len(event_ids) or not set(case["seen_event_ids"]) <= set(
            event_ids
        ):
            raise ValueError("Evidence identity or supplied prior context is inconsistent.")
        for group, bound in ((case["prior_events"], times[1]), (case["events"], times[2])):
            if any(prices.instant(e["available_at"]) > bound or not e["event_key"] for e in group):
                raise ValueError("Future or unidentified evidence in a case.")
        coverage = case["coverage"]
        if coverage["status"] not in ("complete", "incomplete") or not isinstance(
            coverage["gaps"], list
        ):
            raise ValueError("Unknown coverage health.")
        if coverage["checked_through"] and prices.instant(coverage["checked_through"]) > times[2]:
            raise ValueError("Coverage cannot extend beyond the case cutoff.")
        label = labels[case["id"]]
        if (
            not label["acceptable_decisions"]
            or not set(label["acceptable_decisions"]) <= {"continue", "reassess"}
            or not set(label["required_condition_ids"]) <= set(conditions) | {"UNLISTED"}
            or not set(label["required_evidence_ids"]) <= set(event_ids)
        ):
            raise ValueError("Evaluator label refers to an unsupported outcome or ID.")


# Build the selected model's scope-evaluation request from the saved cases.
def request_for(root, model):
    if model not in MODELS:
        raise ValueError("Unknown model profile.")
    watch, cases, labels = inputs(root)
    validate_inputs(watch, cases, labels)
    # Labels, evaluation notes and the other model's answers never enter the request.
    payload = dict(watch=watch, cases=[{k: c[k] for k in CASE_FIELDS} for c in cases])
    return dict(
        model=model,
        instructions=(root / "monitor_scope_prompt.md").read_text(),
        input=dump(payload),
        **SETTINGS,
        text={
            "format": {
                "type": "json_schema",
                "name": "monitor_scope_batch",
                "strict": True,
                "schema": Batch.model_json_schema(),
            }
        },
    )


# Validate one scope decision against the watch and the case evidence.
def check_decision(value, watch, case):
    value = Decision.model_validate(value).model_dump()
    if value["case_id"] != case["id"] or not value["reason"].strip():
        raise ValueError("Missing case identity or explanation.")
    for key, allowed in (
        ("condition_ids", {c["id"] for c in watch["conditions"]} | {"UNLISTED"}),
        ("evidence_ids", {e["id"] for e in case["prior_events"] + case["events"]}),
    ):
        if len(set(value[key])) != len(value[key]) or not set(value[key]) <= allowed:
            raise ValueError("Unknown or repeated supporting ID.")
    if value["decision"] == "reassess":
        if not all(
            (value["condition_ids"], value["evidence_ids"], value["review_question"].strip())
        ):
            raise ValueError("Reassessment requires a premise, evidence and a referral question.")
    elif value["review_question"].strip():
        raise ValueError("Continue must not masquerade as a Big Seven referral.")
    return value


# Combine the news assessment with an independent evaluation of the price observation.
def arbitrate(watch, observation, news=None, state=None):
    """Test adapter: news failure cannot prevent independent qualified price evaluation."""
    mapped, health = None, dict(status="unavailable", gaps=["No accepted news assessment."])
    try:
        if (
            news["watch"]["forecast_id"] != watch["forecast"]["id"]
            or news["watch"]["symbol"] != watch["forecast"]["symbol"]
        ):
            raise ValueError("News belongs to another forecast.")
        value = check_decision(news["decision"], news["watch"], news["case"])
        events = news["case"]["prior_events"] + news["case"]["events"]
        keys = sorted({e["event_key"] for e in events if e["id"] in value["evidence_ids"]})
        health = dict(
            news["case"]["coverage"], status="incomplete" if has_gap(news["case"]) else "complete"
        )
        mapped = dict(
            decision="reassess" if value["decision"] == "reassess" else "quiet", event_ids=keys
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        mapped = None  # Preserve the gap; do not feed malformed news to the price engine.
    result = prices.evaluate(watch, observation, state, mapped)
    return dict(
        decision="reassess" if result["signals"] else "continue",
        execute=False,
        coverage=dict(
            news=health, price=dict(status=result["price_status"], gaps=result["reasons"])
        ),
        price_result=result,
    )


# Score the model's reported decisions against the expected case outcomes.
def evaluate_response(text, root):
    watch, cases, labels = inputs(root)
    values = Batch.model_validate_json(text).model_dump()["decisions"]
    by_id = {v["case_id"]: v for v in values}
    if len(by_id) != len(values) or set(by_id) != {c["id"] for c in cases}:
        raise ValueError("Require exactly one decision per supplied case.")
    rows = []
    for case in cases:
        value, label = check_decision(by_id[case["id"]], watch, case), labels[case["id"]]
        checks = dict(
            decision=value["decision"] in label["acceptable_decisions"],
            conditions=set(label["required_condition_ids"]) <= set(value["condition_ids"]),
            evidence=set(label["required_evidence_ids"]) <= set(value["evidence_ids"]),
            watch=value["decision"] == "reassess"
            or not label["watch_required"]
            or bool(value["watch_for"].strip()),
        )
        rows.append(
            dict(
                case_id=case["id"],
                assessment=value,
                checks=checks,
                checks_passed=all(checks.values()),
                coverage=dict(
                    case["coverage"], status="incomplete" if has_gap(case) else "complete"
                ),
                rubric=label,
                reasoning_review="pending human review",
            )
        )
    return rows


# Write an HTML summary of the scope evaluation and its individual cases.
def render(folder, result):
    body = "<h1>Monitor scope test</h1><p>Controlled synthetic evidence; no executed referrals.</p>"
    body += "<p>Status: <b>" + escape(result["status"]) + "</b></p>"
    body += "<p>Automatic checks do not certify reasoning. Review the explanations and rubric below.</p>"
    for row in result.get("rows", []):
        value = row["assessment"]
        body += "<h2>" + escape(row["case_id"] + ": " + value["decision"]) + "</h2>"
        for title, content in [
            ("Reason", value["reason"]),
            ("Watch for", value["watch_for"]),
            ("Big Seven question", value["review_question"]),
            (
                "Checks / coverage / reviewer rubric",
                dump({k: v for k, v in row.items() if k != "assessment"}),
            ),
        ]:
            body += "<h3>" + escape(title) + "</h3><pre>" + escape(content or "None") + "</pre>"
    body += (
        "<details><summary>Complete saved result</summary><pre>"
        + escape(dump(result))
        + "</pre></details>"
    )
    write(
        folder / "summary.html",
        '<!doctype html><meta charset="utf-8"><title>Monitor scope review</title>'
        "<style>body{max-width:1050px;margin:2em auto;padding:0 1em;font:17px/1.5 system-ui}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:1em}</style>"
        + body,
    )


# Freeze the model request and supporting files for a scope evaluation.
def prepare(folder, model, root=ROOT):
    request = request_for(root, model)
    hashes = {name: digest((root / name).read_bytes()) for name in FILES}
    identity = dict(files=hashes, model=model, request_sha256=digest(dump(request)))
    fingerprint = digest(dump(identity))
    if (folder / "manifest.json").exists():
        if read(folder / "manifest.json")["fingerprint"] != fingerprint:
            raise ValueError(
                "Existing experiment differs; preserve it and use a new version folder."
            )
    else:
        for name in FILES:
            target = folder / "frozen" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / name).read_bytes())
        allowance = (
            (len(dump(request).encode()) + 1024) * max(RATES[model][:-1])
            + SETTINGS["max_output_tokens"] * RATES[model][-1]
        ) / 1_000_000
        write(
            folder / "manifest.json",
            dict(
                identity,
                fingerprint=fingerprint,
                created_at=stamp(),
                schema="monitor-scope-experiment-v1",
                settings=SETTINGS,
                rates=RATES[model],
                planning_estimate_usd=allowance,
                cost_note="Saved short-context rates; conservative input byte proxy plus full output allowance. "
                "Not an invoice or a billing cap. No hosted research fees in this supplied-evidence test.",
            ),
        )
        write(folder / "request.json", request)
        write(folder / "packages.json", {n: version(n) for n in ("openai", "pydantic")})
    verify(folder)
    mechanical = prices.replay(read(folder / "frozen/fixtures/prices/replay.json"))
    write(folder / "mechanical.json", mechanical)
    if mechanical["passed"] != mechanical["total"]:
        raise ValueError("Mechanical replay failed; no model request permitted.")
    if not (folder / "result.json").exists():
        result = dict(status="not_attempted", model=model, execute=False)
        write(folder / "result.json", result)
        render(folder, result)
    print(
        f"Prepared one {model} request with {len(inputs(root)[1])} independent supplied-evidence cases.",
        flush=True,
    )
    print(
        f"Mechanical replay: {mechanical['passed']}/{mechanical['total']}; no model calls made.",
        flush=True,
    )
    print(
        f"Planning estimate ${read(folder / 'manifest.json')['planning_estimate_usd']:.3f}; not a billing cap.",
        flush=True,
    )


# Check the evaluation manifest and recover the exact request that was prepared.
def verify(folder):
    manifest = read(folder / "manifest.json")
    identity = {k: manifest[k] for k in ("files", "model", "request_sha256")}
    if (
        set(manifest["files"]) != set(FILES)
        or digest(dump(identity)) != manifest["fingerprint"]
        or manifest["settings"] != SETTINGS
        or manifest["rates"] != list(RATES[manifest["model"]])
    ):
        raise ValueError("Experiment manifest changed.")
    for name, expected in manifest["files"].items():
        if digest((folder / "frozen" / name).read_bytes()) != expected:
            raise ValueError("Frozen experiment file changed: " + name)
    if digest(Path(__file__).read_bytes()) != manifest["files"]["monitor_scope.py"]:
        raise ValueError("Use the saved version of this runner.")
    request = request_for(folder / "frozen", manifest["model"])
    if (
        digest(dump(request)) != manifest["request_sha256"]
        or read(folder / "request.json") != request
    ):
        raise ValueError("Request does not match the frozen inputs.")
    return manifest, request


# Run or reuse the scope evaluation and persist its scored result.
def run(folder, client=None, *, verifier=verify, evaluator=evaluate_response, renderer=render):
    manifest, request = verifier(folder)
    with (folder / ".run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (folder / "attempt.json").exists():
            print("This model test was already attempted; no new request.", flush=True)
            return read(folder / "result.json")
        if client is None:
            from openai import OpenAI

            if not os.environ.get("OPENAI_API_KEY"):
                raise ValueError("Approved capstone credential unavailable.")
            client = OpenAI(max_retries=0, timeout=90)
        result = dict(
            status="attempted", model=manifest["model"], started_at=stamp(), execute=False
        )
        write(folder / "attempt.json", result)
        start = monotonic()
        try:
            print(
                "Calling "
                + manifest["model"]
                + " once; supplied evidence, "
                + request["reasoning"]["effort"]
                + " reasoning, shadow decisions.",
                flush=True,
            )
            response = client.responses.create(**request)
            raw = response.model_dump(mode="json")
            write(folder / "response.json", raw)
            while raw.get("status") in ("queued", "in_progress"):
                write(
                    folder / "attempt.json",
                    dict(result, response_id=raw["id"], provider_status=raw["status"]),
                )
                print(
                    "Provider status: " + raw["status"] + "; observing the same generation.",
                    flush=True,
                )
                sleep(5)
                response = client.responses.retrieve(raw["id"])
                raw = response.model_dump(mode="json")
                write(folder / "response.json", raw)
            result.update(
                response_id=raw.get("id"),
                provider_status=raw.get("status"),
                provider_model=raw.get("model"),
                usage=raw.get("usage"),
                unaccepted_output=response.output_text,
            )
            try:
                if (
                    raw.get("service_tier") not in (None, "default")
                    or raw["usage"]["input_tokens"] > 272000
                ):
                    raise ValueError("Usage requires manual reconciliation.")
                if raw.get("model") != manifest["model"] and not (
                    raw.get("model") or ""
                ).startswith(manifest["model"] + "-"):
                    raise ValueError("Unexpected model.")
                result["cost"] = dict(
                    token_estimate_usd=token_cost(
                        raw["usage"], "coached", rates=RATES[manifest["model"]]
                    ),
                    note="Saved-rate estimate, not an invoice. Unknown cache-write duration uses the higher saved rate.",
                )
            except (KeyError, TypeError, ValueError):
                result["cost"] = dict(
                    note="Usage requires manual cost reconciliation; raw response retained."
                )
            if raw.get("status") != "completed":
                raise ValueError("Provider did not complete a final answer.")
            if raw.get("model") != manifest["model"] and not (raw.get("model") or "").startswith(
                manifest["model"] + "-"
            ):
                raise ValueError("Response came from an unexpected model.")
            if any(i.get("type", "").endswith("_call") for i in raw.get("output", [])):
                raise ValueError("Unexpected tool operation in the supplied-evidence test.")
            result["rows"] = evaluator(response.output_text, folder / "frozen")
            result.update(
                status="completed",
                automatic_checks_passed=sum(r["checks_passed"] for r in result["rows"]),
                cases=len(result["rows"]),
                reasoning_review="pending human review",
            )
            result.pop("unaccepted_output")
        except Exception as error:
            result.update(
                status="failed",
                error_type=type(error).__name__,
                note="No automatic retry. This attempt remains consumed; inspect saved response and inputs.",
            )
            if type(error) is ValueError:
                result["validation_error"] = str(error)
            if "usage" not in result:
                result["cost"] = dict(
                    note="Final usage unknown; the provider may still be running. Do not repeat generation."
                )
        result.update(finished_at=stamp(), elapsed_seconds=round(monotonic() - start, 3))
        write(folder / "result.json", result)
        renderer(folder, result)
        print(
            "Scope test " + result["status"] + "; HTML: " + str(folder / "summary.html"), flush=True
        )
        if result["status"] == "completed":
            print(
                f"Automatic rubric checks: {result['automatic_checks_passed']}/{result['cases']}; reasoning review pending.",
                flush=True,
            )
        return result


# Dispatch the command to prepare, verify, or run a scope evaluation.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, default=MODELS[0])
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output.resolve(), args.model)
    elif run(args.output.resolve())["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
