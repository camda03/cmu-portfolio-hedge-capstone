"""Prepare one audited citation correction for the unchanged, saved research runner."""

import argparse
import fcntl
import hashlib
import json
from pathlib import Path

import research
from research_disclosure import disclosure
from research_evidence import load_evidence


# Reject the repair when a required piece of evidence fails validation.
def check(condition, message):
    if not condition:
        raise ValueError(message)


# Fingerprint the exact bytes of the evidence or response being repaired.
def sha(data):
    return hashlib.sha256(data).hexdigest()


# Tie the citation correction to the rejected response and its original evidence.
def prepare_repair(folder):
    """Bind a citation correction to its rejected response and original evidence before another request."""
    folder = Path(folder).resolve()
    evidence = load_evidence(folder)
    with (folder / ".research.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path, receipt_path = folder / "research_state.json", folder / "research_receipt.json"
        before, receipt_before = state_path.read_bytes(), receipt_path.read_bytes()
        state, receipt = json.loads(before), json.loads(receipt_before)
        config = state["config"]
        expected = {
            "input_sha256": evidence["input_sha256"],
            "model": config["model"],
            "reasoning": config["reasoning"],
            "max_rounds": config["max_rounds"],
            "disclosure": disclosure(evidence),
            "prompt_sha256": sha((research.ROOT / "research_prompt.md").read_bytes()),
            "runner_sha256": sha(Path(research.__file__).read_bytes()),
            "report_schema": research.ResearchReport.model_json_schema(),
            "tools": research.TOOLS,
        }
        check(
            config == expected and state["signature"] == research.digest(expected),
            "Saved research settings, runner, prompt or evidence changed; no correction prepared.",
        )
        check(
            receipt.get("config_sha256") == state["signature"]
            and receipt.get("run_id") == evidence["summary"]["run_id"]
            and receipt.get("input_sha256") == evidence["input_sha256"],
            "Research receipt differs from this run.",
        )
        journal = folder / "research_citation_repair.json"
        if journal.exists():
            audit = json.loads(journal.read_bytes())
            check(
                audit["signature"] == state["signature"]
                and audit["input_sha256"] == evidence["input_sha256"]
                and sha((folder / audit["response_file"]).read_bytes()) == audit["response_sha256"],
                "Saved correction or rejected response differs; retain the run.",
            )
            if sha(before) == audit["state_before_sha256"]:
                research.save(
                    state_path, audit["state_after"]
                )  # Recover an interrupted journal commit.
            else:
                check(
                    state["step"] >= audit["state_after"]["step"]
                    and audit["rejected_response_id"] in state["response_ids"],
                    "Unexpected correction state.",
                )
            return audit  # Never reset queued requests or prepare another correction.

        check(
            receipt.get("status") == "failed"
            and receipt.get("complete") is False
            and receipt.get("error_type") == "ValueError",
            "Only a failed validation is eligible for this repair.",
        )
        check(
            not state["submitting"] and state["response_id"],
            "Uncertain submission; preserve its provider state.",
        )
        response_file = f"research_response_{state['step']:03}.json"
        raw_bytes = (folder / response_file).read_bytes()
        raw = json.loads(raw_bytes)
        check(
            raw["id"] == state["response_id"]
            and raw["status"] == "completed"
            and raw.get("metadata")
            == {"gdelt_signature": state["signature"], "gdelt_step": str(state["step"])}
            and not any(item["type"] == "function_call" for item in raw["output"]),
            "Expected this run's completed final response; no correction prepared.",
        )
        text = "".join(
            part["text"]
            for item in raw["output"]
            if item["type"] == "message"
            for part in item.get("content", [])
            if part["type"] == "output_text"
        )
        report = research.ResearchReport.model_validate_json(text)
        seen = {tuple(pair) for pair in state["seen_pairs"]}
        known = research.pairs(evidence["articles"]["cases"])
        dates = {row["observed_day"] for row in evidence["paths"]["date_rows"]}
        errors = []
        for section in ("analogues", "alternatives"):
            for index, analogue in enumerate(getattr(report, section)):
                where = {"section": section, "index": index, "date": analogue.date}
                if analogue.date not in dates or not analogue.evidence_refs:
                    errors.append(
                        where
                        | {"problem": "Date absent from histogram or no supporting citations."}
                    )
                for ref in analogue.evidence_refs:
                    key = (ref.query_document_id, ref.historical_document_id)
                    if key not in seen or known.get(key) != analogue.date:
                        errors.append(
                            where
                            | ref.model_dump()
                            | {
                                "pair_exists": key in known,
                                "pair_was_inspected": key in seen,
                                "saved_observed_day": known.get(key),
                            }
                        )
        check(errors, "No invalid citations found; this helper does not repair other failures.")
        examples = {
            day: [
                {"query_document_id": q, "historical_document_id": h}
                for q, h in sorted(seen)
                if known.get((q, h)) == day
            ][:5]
            for day in sorted({a.date for a in [*report.analogues, *report.alternatives]})
        }
        feedback = {
            "instruction": "Your previous report failed citation validation and was not published. "
            "Correct the full report using exact supplied IDs and historical observed_day values. "
            "Use read_article_matches to inspect any missing support. Examples are lookup guidance, "
            "not automatic support for your claims: reassess the explanations when support changes. "
            "Remove unsupported analogues or alternatives and disclose the limitation. Do not invent IDs, "
            "relabel dates, or replace a citation without checking its meaning. Return the complete report schema.",
            "validation_errors": errors,
            "inspected_examples_up_to_five_per_reported_date": examples,
        }
        after = state | {
            "step": state["step"] + 1,
            "previous_response_id": raw["id"],
            "response_id": None,
            "submitting": False,
            "next_input": [{"role": "user", "content": research.encode(feedback)}],
            "response_ids": [*state["response_ids"], raw["id"]],
            "usage": [*state["usage"], raw.get("usage")],
        }
        audit = {
            "schema": "gdelt-research-citation-repair-v1",
            "signature": state["signature"],
            "input_sha256": evidence["input_sha256"],
            "rejected_response_id": raw["id"],
            "response_file": response_file,
            "response_sha256": sha(raw_bytes),
            "validation_errors": errors,
            "repair_code_sha256": sha(Path(__file__).read_bytes()),
            "state_before_sha256": sha(before),
            "state_backup_file": "research_state.before_citation_repair.json",
            "receipt_backup_file": "research_receipt.before_citation_repair.json",
            "state_after": after,
        }
        for name, data in (
            (audit["state_backup_file"], before),
            (audit["receipt_backup_file"], receipt_before),
        ):
            target = folder / name
            if target.exists():
                check(target.read_bytes() == data, "Existing correction backup differs; retain it.")
            else:
                with target.open("xb") as handle:
                    handle.write(data)
        research.save(journal, audit)
        research.save(state_path, after)
        return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    audit = prepare_repair(args.folder)
    print(
        research.encode(
            {
                "state": "CITATION_CORRECTION_PREPARED",
                "rejected_response_id": audit["rejected_response_id"],
                "validation_errors": audit["validation_errors"],
            }
        ),
        flush=True,
    )
