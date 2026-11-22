"""Read the saved GDELT delivery and the seven-report portfolio feed."""

from hashlib import sha256
import json

from ledger import instant


def require(condition, message):
    if not condition:
        raise ValueError(message)


def gdelt_input(folder, cutoff, allow_fixture):
    def read(name):
        return json.loads((folder / name).read_bytes())

    delivery_bytes = (folder / "handoff.json").read_bytes()
    handoff = json.loads(delivery_bytes)
    require(
        handoff["schema"] == "gdelt-research-handoff-v2" and handoff["status"] == "completed",
        "Expected a completed GDELT v2 delivery.",
    )
    receipt_bytes = (folder / "receipt.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    digest = sha256(receipt_bytes).hexdigest()
    run = handoff["run_id"]
    manifest = receipt["files"]
    require(
        run
        and receipt["run_id"] == receipt["source_receipt"]["run_id"] == run
        and receipt["schema"] == "gdelt-research-evidence-v1"
        and receipt["complete"] is True
        and handoff["input_sha256"] == digest,
        "GDELT evidence receipt or run binding failed.",
    )
    require(
        set(manifest) == {"summary.json", "articles.json", "paths.json"}
        and manifest == handoff["evidence_files"],
        "GDELT evidence manifest mismatch.",
    )
    for name, expected in manifest.items():
        raw = (folder / name).read_bytes()
        require(sha256(raw).hexdigest() == expected, f"Evidence hash mismatch in {name}.")
        evidence = json.loads(raw)
        require(evidence["run_id"] == run, f"Evidence run mismatch in {name}.")
        if name == "summary.json":
            require(
                evidence == handoff["evidence_summary"],
                "Embedded summary differs from verified summary.",
            )

    report, scope = handoff["research"], handoff["disclosure"]
    provenance = handoff["research_provenance"]
    original = provenance["source_research_receipt"]
    require(
        report["schema"] == "gdelt-research-view-v2"
        and report["overall_assessment"]
        and scope["schema"] == "gdelt-research-disclosure-v1"
        and scope == read("disclosure.json")
        and scope["selection"]
        and scope["model_review"]
        and scope["limitations"]
        and scope["observation_window"]["mode"] == "saved_snapshot",
        "Missing research or inconsistent saved-cohort disclosure.",
    )
    require(
        all(
            item["run_id"] == run and item["input_sha256"] == digest
            for item in (original, report, scope)
        )
        and original["complete"] is True
        and original["status"] == "completed"
        and original["research_sha256"] == provenance["source_research_sha256"],
        "Research or disclosure belongs to another run or receipt.",
    )
    for policy in (handoff["policy"], scope["downstream_use"]):
        require(
            policy["authority"] == "advisory"
            and policy["trade_authority"] is False
            and policy["automatic_source_precedence"] is False
            and policy["conflict_policy"] == "case_by_case_evidence_weighting",
            "GDELT policy conflicts with discretionary source weighting.",
        )
    fixture = bool(
        receipt["source_receipt"].get("synthetic")
        or handoff["evidence_summary"]["context"].get("synthetic")
    )
    require(allow_fixture or not fixture, "Synthetic GDELT evidence cannot establish a real rehearsal.")
    window = scope["observation_window"]
    require(
        handoff["delivery_created_at"] and report["created_at"], "Missing delivery or report timestamp."
    )
    for stamp in (handoff["delivery_created_at"], report["created_at"], window["end"], window["as_of"]):
        if stamp:
            require(instant(stamp) <= cutoff, "GDELT input postdates decision time.")
    return handoff, sha256(delivery_bytes).hexdigest(), fixture
