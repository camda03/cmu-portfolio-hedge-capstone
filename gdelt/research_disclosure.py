"""Factual scope disclosures derived from saved evidence, never from model prose."""
from copy import deepcopy


def count(value, label, unit, source, note=""):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Invalid recorded count: " + label)
    return {"value": value, "label": label, "unit": unit, "source": source,
            "status": "recorded" if value is not None else "not_recorded",
            "note": note if value is not None else "Not recorded in this evidence packet; unknown, not zero. " + note}


def disclosure(packet, research_receipt=None):
    summary, receipt = packet["summary"], research_receipt or {}
    context = summary.get("context", {})
    intake = context.get("intake", {})
    measured, source = intake.get("counts", {}), intake.get("source_receipt", {})
    counts = {}
    for key, label, unit in [
        ("source_rows", "GKG rows observed in the source window", "GKG rows"),
        ("qualifying_web_rows", "Eligible web-source rows before sampling", "GKG rows"),
        ("distinct_qualifying_raw_revisions", "Distinct eligible revisions before sampling", "raw revisions")]:
        counts[key] = count(source.get(key), label, unit, "summary.context.intake.source_receipt." + key)
    counts["raw_limit"] = count(intake.get("raw_limit"), "Configured raw sample ceiling", "raw revisions",
        "summary.context.intake.raw_limit", "A ceiling, not an observed sample size or the total source population.")
    for key, label, unit in [
        ("sampled_raw_revisions", "Raw revisions actually sampled", "raw revisions"),
        ("normalized_revisions", "Revisions processed by normalization", "raw revisions"),
        ("admissible_revisions", "Revisions passing normalization", "raw revisions"),
        ("normalization_rejected_revisions", "Revisions rejected by normalization", "raw revisions"),
        ("selected_canonical_documents", "Selected URL-based document representatives", "article identities"),
        ("expanded_documents", "Documents passing Expanded rules", "article identities"),
        ("excluded_no_match_documents", "Documents excluded for no Expanded rule match", "article identities"),
        ("excluded_no_theme_documents", "Documents excluded for missing themes", "article identities"),
        ("expanded_history_overlaps", "Qualifying documents already in history", "article identities"),
        ("expanded_fresh_documents", "Qualifying fresh documents before final selection", "article identities")]:
        counts[key] = count(measured.get(key), label, unit, "summary.context.intake.counts." + key)
    counts["query_documents"] = count(summary.get("query_documents"), "Current stories in the matched cohort",
        "article identities", "summary.query_documents", "Distinct article identities need not be distinct real-world events.")
    return {
        "schema": "gdelt-research-disclosure-v1", "run_id": summary.get("run_id"),
        "input_sha256": packet.get("input_sha256"), "generated_by": "application_from_saved_records",
        "observation_window": {"start": intake.get("window_start", context.get("window_start")),
            "end": intake.get("window_end", context.get("window_end")),
            "as_of": intake.get("as_of_utc", context.get("source_as_of_utc", context.get("as_of_utc"))),
            "source": "saved summary.context and summary.context.intake", "mode": "saved_snapshot"},
        "selection": {"counts": counts, "representativeness": "not_established",
            "method": ("Deterministic seeded sampling of raw revisions; normalization and URL-based representative selection; Expanded metadata rules; exclusion of historical-document identities; deterministic selection of the final query cohort."
                if context.get("purpose") == "fresh_gkg_demo" else "Selection procedure is not established by this packet's purpose; retain recorded counts and consult its source-run configuration."),
            "source_coverage": "Counts describe the saved source snapshot; completeness of all published news and late-arriving records is not established.",
            "denominators": "Raw revisions, canonical article identities and distinct events are different units. No query-count/raw-revision ratio is presented as a document retention rate or full-feed coverage rate."},
        "historical_corpus": {"documents": count(summary.get("historical_documents"), "Historical reference corpus",
            "article identities", "summary.historical_documents"),
            "scope": "Selected historical reference sample, not a comprehensive archive of all GDELT coverage or market-relevant events.",
            "sampling_amendment": deepcopy(context.get("sampling_amendment"))},
        "model_review": {"status": receipt.get("status", "not_recorded"),
            "delivered_query_records": count(receipt.get("inspected_query_count"), "Query records delivered through model tools",
                "article identities", "research_receipt.inspected_query_count"),
            "delivered_market_dates": deepcopy(receipt.get("inspected_market_dates")),
            "interpretation": "Tool delivery is recorded exposure, not proof of deep review of every record. The initial index includes current titles and identifiers; detailed pairs are obtained through tools."},
        "strengths": ["Uses saved, inspectable retrieval evidence and recorded historical market paths.",
            "Retains exact input/run provenance, original vote weights and numerical paths.",
            "Research analogues cite delivered query/historical article pairs; weak or insufficient resemblance is permitted."],
        "limitations": [
            "The selected cohort and filtered historical corpus are not established as statistically representative; no population inference or sampling confidence interval is claimed.",
            "Source counts, when absent, remain unknown. A configured ceiling must never be described as an observed denominator or complete feed coverage.",
            "Evidence contains GKG metadata and available titles, not full article bodies. Metadata agreement does not verify the underlying story or event.",
            "Only displayed cosine, BM25 and RRF representatives are provided as pairs; not all candidates or tied winners. Histogram vote mass nevertheless includes tied highest-RRF winners.",
            "Repeated coverage of one event, shared historical dates and overlapping return windows create dependence. Story count is not the number of independent trials.",
            "RRF and histogram weights are retrieval rankings and query votes, not calibrated confidence or event probabilities. No match-quality reweighting is applied.",
            "S&P 500 paths are daily-close price returns, excluding dividends, from the first trading close after the GKG date through 20 subsequent trading sessions. They do not measure intraday extremes.",
            "The weighted path is descriptive historical evidence. Predictive skill, causality, expected trading returns, confidence intervals and prediction intervals have not been established.",
            "The research narrative is model interpretation, not an independently verified finding. Relationships and market context can change over time."],
        "downstream_use": {"authority": "advisory", "conflict_policy": "case_by_case_evidence_weighting",
            "automatic_source_precedence": False, "trade_authority": False,
            "instruction": "Evaluate strengths, limitations, freshness and corroboration alongside other research. Use, downweight, combine or decline this evidence; no source receives automatic precedence."}}
