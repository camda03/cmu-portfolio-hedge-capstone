"""One GDELT research role over a verified, saved evidence packet."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Literal

from openai import APIStatusError, OpenAI
from pydantic import BaseModel, ConfigDict
from research_evidence import load_evidence
from research_disclosure import disclosure

ROOT = Path(__file__).resolve().parent


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceRef(Record):
    query_document_id: str
    historical_document_id: str


class Alternative(Record):
    date: str
    assessment: str
    evidence_refs: list[EvidenceRef]


class Analogue(Alternative):
    similarities: list[str]
    differences: list[str]


class ResearchReport(Record):
    headline: str
    overall_assessment: str
    resemblance: Literal["broad", "partial", "mixed", "insufficient"]
    current_themes: list[str]
    analogues: list[Analogue]
    alternatives: list[Alternative]
    uncovered_themes: list[str]
    market_interpretation: str
    implications: list[str]
    limitations: list[str]
    questions: list[str]


def strings():
    return {"type": "array", "items": {"type": "string"}}


def tool(name, description, properties):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


TOOLS = [
    tool("read_article_matches", "Read original current/historical metadata and channel winners. "
         "Empty filter arrays mean unrestricted; nonempty filters are intersected. "
         "Date filter matches any displayed cosine, BM25 or RRF winner. Use pagination to inspect more.",
         {"offset": {"type": "integer", "minimum": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 25},
          "query_document_ids": strings(), "historical_dates": strings()}),
    tool("read_market_paths", "Read exact T0–T20 price-return paths for selected historical GKG dates, "
         "plus the unchanged full-cohort weighted path. Assess article resemblance before using this tool.",
         {"observed_days": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}}),
]


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encode(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def pairs(cases):
    return {(case["query"]["document_id"], record["document_id"]): record["observed_day"]
            for case in cases for channel in ("cosine", "bm25", "fused")
            if (record := case.get(channel)) and record.get("document_id")}


def read_tool(evidence, name, arguments):
    """Only immutable packet records are readable; there are no SQL, network or order tools."""
    if name == "read_article_matches":
        offset, limit = arguments["offset"], arguments["limit"]
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 25:
            raise ValueError("Use a nonnegative offset and a page size from 1 to 25.")
        ids, dates = set(arguments["query_document_ids"]), set(arguments["historical_dates"])
        cases = [case for case in evidence["articles"]["cases"]
                 if (not ids or case["query"]["document_id"] in ids) and
                 (not dates or any((case.get(channel) or {}).get("observed_day") in dates
                                   for channel in ("cosine", "bm25", "fused")))]
        page = cases[offset:offset + limit]
        return {"total_cases": len(cases), "offset": offset, "cases": page,
                "next_offset": offset + len(page) if offset + len(page) < len(cases) else None}
    if name == "read_market_paths":
        dates = arguments["observed_days"]
        paths = {row["observed_day"]: row for row in evidence["paths"]["date_rows"]}
        if not 1 <= len(dates) <= 20 or len(set(dates)) != len(dates) or not set(dates) <= paths.keys():
            raise ValueError("Request 1–20 distinct dates present in the histogram.")
        return {"date_rows": [paths[day] for day in dates],
                "weighted_price_return_path": evidence["summary"]["weighted_price_return_path"],
                "interpretation": evidence["summary"]["interpretation"]}
    raise ValueError("Unknown evidence tool.")


def overview(evidence):
    summary = {key: value for key, value in evidence["summary"].items()
               if key != "weighted_price_return_path"}
    summary["current_articles"] = [
        {key: case["query"].get(key) for key in ("document_id", "title", "observed_day")}
        for case in evidence["articles"]["cases"]]
    summary["disclosure"] = disclosure(evidence)
    return summary


def validate_report(report, evidence, seen_pairs):
    known = pairs(evidence["articles"]["cases"])
    dates = {row["observed_day"] for row in evidence["paths"]["date_rows"]}
    if known and not seen_pairs:
        raise ValueError("Inspect supplied article pairs before assessing resemblance.")
    if report.resemblance != "insufficient" and not report.analogues:
        raise ValueError("A substantive resemblance assessment needs a supported leading analogue.")
    for analogue in [*report.analogues, *report.alternatives]:
        if analogue.date not in dates or not analogue.evidence_refs:
            raise ValueError("An analogue needs a supplied date and inspectable supporting pairs.")
        for ref in analogue.evidence_refs:
            key = (ref.query_document_id, ref.historical_document_id)
            if key not in seen_pairs or known.get(key) != analogue.date:
                raise ValueError("An analogue cites an unread, nonexistent or wrong-date article pair.")


def analyze(evidence_dir, output, model="gpt-6.2-astra", reasoning="max", client=None, resume_response_id=None, max_rounds=128):
    if type(max_rounds) is not int or max_rounds < 1:
        raise ValueError("The runaway backstop --max-rounds must be a positive integer.")
    evidence = load_evidence(evidence_dir)
    prompt = (ROOT / "research_prompt.md").read_text(encoding="utf-8")
    config = {"input_sha256": evidence["input_sha256"], "model": model, "reasoning": reasoning, "max_rounds": max_rounds,
              "disclosure": disclosure(evidence),
              "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "report_schema": ResearchReport.model_json_schema(), "tools": TOOLS}
    signature = digest(config)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".research.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path, receipt_path = output / "research_state.json", output / "research_receipt.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "signature": signature, "config": config, "step": 0, "response_id": None,
            "submitting": False, "previous_response_id": None, "response_ids": [], "usage": [],
            "seen_pairs": [], "market_dates": [], "next_input": [{"role": "developer", "content": prompt},
                {"role": "user", "content": encode(overview(evidence))}]}
        if state["signature"] != signature:
            raise ValueError("Evidence, model or prompt changed. Preserve this run and choose a new output directory.")
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if receipt and (receipt.get("run_id") != evidence["summary"]["run_id"]
                        or receipt.get("input_sha256") != evidence["input_sha256"]):
            raise ValueError("Research receipt belongs to different evidence.")
        if receipt.get("complete") is True:
            if receipt.get("config_sha256") != signature:
                raise ValueError("Completed research uses different settings; preserve it and use a new output directory.")
            report_path = output / "research.json"
            if receipt.get("status") != "completed" or hashlib.sha256(report_path.read_bytes()).hexdigest() != receipt["research_sha256"]:
                raise ValueError("Completed research checksum mismatch.")
            result = json.loads(report_path.read_text())
            if any(result.get(key) != value for key, value in {
                    "schema": "gdelt-research-report-v2", "run_id": receipt["run_id"],
                    "input_sha256": evidence["input_sha256"], "authority": "advisory", "automatic_source_precedence": False}.items()):
                raise ValueError("Completed research envelope mismatch.")
            print(encode({"state": "RESEARCH_REUSED", "run_id": evidence["summary"]["run_id"]}), flush=True)
            return result
        save(state_path, state)
        receipt = {"schema": "gdelt-research-receipt-v1", "run_id": evidence["summary"]["run_id"],
                   "input_sha256": evidence["input_sha256"], "requested_model": model,
                   "config_sha256": signature,
                   "reasoning_effort": reasoning, "status": "running", "complete": False}
        save(receipt_path, receipt)
        try:
            client = client or OpenAI(max_retries=0, timeout=60)
            while True:
                response_path = output / f"research_response_{state['step']:03}.json"
                raw = json.loads(response_path.read_text()) if response_path.exists() else None
                response_id = resume_response_id or state["response_id"] or (raw or {}).get("id")
                if response_id:
                    if not raw or raw["id"] != response_id or raw["status"] != "completed":
                        raw = client.responses.retrieve(response_id).model_dump(mode="json")
                    resume_response_id = None
                else:
                    if state["step"] >= max_rounds:
                        raise ValueError("Research exceeded the provider-request runaway backstop; saved without another request.")
                    if state["submitting"]:
                        raise ValueError("Prior submission outcome unknown; no replay. Recover its provider response ID "
                                         "with --resume-response-id after reviewing the provider request log.")
                    state["submitting"] = True
                    save(state_path, state)
                    try:
                        raw = client.responses.create(model=model, input=state["next_input"], tools=TOOLS,
                            previous_response_id=state["previous_response_id"], background=True, store=True,
                            reasoning={"effort": reasoning}, max_output_tokens=128000,
                            metadata={"gdelt_signature": signature, "gdelt_step": str(state["step"])},
                            text={"format": {"type": "json_schema", "name": "gdelt_research",
                                  "strict": True, "schema": config["report_schema"]}}).model_dump(mode="json")
                    except APIStatusError as error:
                        if error.status_code in (400, 401, 403, 404, 422, 429):
                            state["submitting"] = False  # Explicit rejection; a later manual retry is safe.
                            save(state_path, state)
                        raise
                if raw.get("metadata") != {"gdelt_signature": signature, "gdelt_step": str(state["step"])}:
                    raise ValueError("Provider response does not belong to this saved research request.")
                state.update(response_id=raw["id"], submitting=False)
                save(response_path, raw)
                save(state_path, state)
                while raw["status"] in ("queued", "in_progress"):
                    print(encode({"state": "RESEARCH_WAITING", "response_id": raw["id"], "step": state["step"]}), flush=True)
                    time.sleep(5)
                    raw = client.responses.retrieve(raw["id"]).model_dump(mode="json")
                    save(response_path, raw)
                if raw["status"] != "completed":
                    raise ValueError("Provider response " + raw["status"] + "; saved without replay.")
                calls = [item for item in raw["output"] if item["type"] == "function_call"]
                if calls:
                    answers, seen = [], {tuple(pair) for pair in state["seen_pairs"]}
                    for call in calls:
                        try:
                            answer = read_tool(evidence, call["name"], json.loads(call["arguments"]))
                            seen.update(pairs(answer.get("cases", [])))
                            state["market_dates"] = sorted(set(state["market_dates"]) |
                                                           {row["observed_day"] for row in answer.get("date_rows", [])})
                        except (ValueError, KeyError, TypeError) as error:
                            answer = {"error": str(error)}
                        answers.append({"type": "function_call_output", "call_id": call["call_id"], "output": encode(answer)})
                    state.update(step=state["step"] + 1, previous_response_id=raw["id"], response_id=None,
                        next_input=answers, seen_pairs=sorted(seen),
                        response_ids=[*state["response_ids"], raw["id"]], usage=[*state["usage"], raw.get("usage")])
                    save(state_path, state)
                    continue
                text = "".join(part["text"] for item in raw["output"] if item["type"] == "message"
                               for part in item.get("content", []) if part["type"] == "output_text")
                report = ResearchReport.model_validate_json(text)
                validate_report(report, evidence, {tuple(pair) for pair in state["seen_pairs"]})
                review = {"status": "completed", "inspected_query_count": len({pair[0] for pair in state["seen_pairs"]}),
                          "inspected_market_dates": state["market_dates"]}
                result = report.model_dump() | {"schema": "gdelt-research-report-v2",
                    "run_id": receipt["run_id"], "input_sha256": evidence["input_sha256"],
                    "created_at": datetime.now(timezone.utc).isoformat(), "model": raw["model"],
                    "authority": "advisory", "automatic_source_precedence": False,
                    "disclosure": disclosure(evidence, review)}
                save(output / "research.json", result)
                receipt.update(status="completed", complete=True, created_at=result["created_at"], model=raw["model"],
                    response_ids=[*state["response_ids"], raw["id"]], usage=[*state["usage"], raw.get("usage")],
                    inspected_query_count=len({pair[0] for pair in state["seen_pairs"]}),
                    inspected_market_dates=state["market_dates"],
                    research_sha256=hashlib.sha256((output / "research.json").read_bytes()).hexdigest())
                save(receipt_path, receipt)
                print(encode({"state": "RESEARCH_COMPLETE", "run_id": receipt["run_id"], "model": raw["model"]}), flush=True)
                return result
        except BaseException as error:
            receipt.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error_type=type(error).__name__,
                           http_status=getattr(error, "status_code", None), provider_error_code=getattr(error, "code", None),
                           provider_request_id=getattr(error, "request_id", None))
            save(receipt_path, receipt)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default="gpt-6.2-astra")
    parser.add_argument("--reasoning", choices=["low", "medium", "high", "xhigh", "max"], default="max")
    parser.add_argument("--max-rounds", type=int, default=128, help="Provider-request runaway backstop; not an article limit.")
    parser.add_argument("--resume-response-id", help="Adopt only a provider response matching this saved request.")
    args = parser.parse_args()
    analyze(args.evidence.resolve(), (args.output or args.evidence).resolve(), args.model, args.reasoning,
            resume_response_id=args.resume_response_id, max_rounds=args.max_rounds)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(encode({"state": "RESEARCH_STOPPED", "error_type": type(error).__name__,
                      "http_status": getattr(error, "status_code", None), "provider_error_code": getattr(error, "code", None),
                      "provider_request_id": getattr(error, "request_id", None),
                      "detail": str(error) if isinstance(error, ValueError) else "See saved state/provider status; no automatic replay."}), flush=True)
        raise SystemExit(1)
