"""Read the saved GDELT delivery and the seven-report portfolio feed."""

from hashlib import sha256
import json

from ledger import instant


# Stop the handoff when a timing, integrity, or evidence requirement is unmet.
def require(condition, message):
    if not condition:
        raise ValueError(message)


# Verify the GDELT delivery, its dates, and the hashes of its evidence files.
def gdelt_input(folder, cutoff, allow_fixture):
    # Read a named JSON record from the supplied GDELT delivery directory.
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
    digest = sha256(receipt_bytes).hexdigest()  # Hash the published bytes before decoding the JSON.
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
    require(
        allow_fixture or not fixture, "Synthetic GDELT evidence cannot establish a real rehearsal."
    )
    window = scope["observation_window"]
    require(
        handoff["delivery_created_at"] and report["created_at"],
        "Missing delivery or report timestamp.",
    )
    for stamp in (
        handoff["delivery_created_at"],
        report["created_at"],
        window["end"],
        window["as_of"],
    ):
        if stamp:
            require(instant(stamp) <= cutoff, "GDELT input postdates decision time.")
    return handoff, sha256(delivery_bytes).hexdigest(), fixture


# Verify the seven archived analyst reports against the decision cutoff.
def archived_analysts(analyst_file, cutoff):
    raw = analyst_file.read_bytes()
    feed = json.loads(raw)
    require(
        feed["schema"] == "portfolio-research-v1"
        and feed["execute"] is False
        and feed["all_reports_available"] is True
        and feed["fleet"]["test_id"]
        and set(feed["subjects"]) == {"AAPL", "DE", "DELL", "MSFT", "NVDA", "WMT", "SP500"},
        "Expected the complete seven-subject portfolio research feed.",
    )
    require(instant(feed["generated_at"]) <= cutoff, "Analyst feed postdates decision time.")
    for symbol, subject in feed["subjects"].items():
        forecast = subject["forecast"]
        require(
            subject["symbol"] == forecast["symbol"] == symbol
            and subject["status"] == "available"
            and subject["report_markdown"].strip()
            and forecast["forecast_id"],
            f"Missing accepted research for {symbol}.",
        )
        require(
            instant(forecast["issued_at"]) <= cutoff
            and instant(forecast["information_cutoff"]) <= cutoff,
            f"Forecast postdates decision time for {symbol}.",
        )
        monitor = subject["last_completed_monitor"]
        if monitor:
            require(
                monitor["symbol"] == symbol and instant(monitor["received_at"]) <= cutoff,
                f"Monitor identity or decision time mismatch for {symbol}.",
            )
        if monitor and monitor["applies_to_active_forecast"]:
            require(
                monitor["forecast_id"] == forecast["forecast_id"], f"Misbound monitor for {symbol}."
            )
    return feed, sha256(raw).hexdigest()


# Combine verified analyst and GDELT evidence for the portfolio under review.
def paired_research(folder, analyst_file, book, cutoff, allow_fixture=False):
    handoff, delivery_hash, fixture = gdelt_input(folder, cutoff, allow_fixture)
    feed, analyst_hash = archived_analysts(analyst_file, cutoff)
    return {
        "schema": "portfolio-paired-research-v1",
        "as_of": cutoff.isoformat(),
        "synthetic_gdelt": fixture,
        "execute": False,
        "decision_instruction": (
            "Assess the broad market and the held basket separately. Explain whether you use, downweight, "
            "combine or decline each source. Cite the GDELT run, its observation window and limitations, "
            "and relevant analyst forecast IDs, horizons and SOL coverage. Recheck age at decision time. "
            "Treat historical return paths as descriptive. Record the implications for the simulated "
            "hedge and what would change your view. This evidence read supplies no execution quote."
        ),
        "portfolio": book,
        "analysts": feed,
        "gdelt": handoff,
        "input_sha256": {"handoff.json": delivery_hash, "portfolio_research": analyst_hash},
    }
