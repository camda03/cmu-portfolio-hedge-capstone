"""Verify evidence from stored BigQuery reports and publish the reader view and hedge handoff."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from research_disclosure import disclosure

NOTES = ("Historical retrieval evidence, not event probabilities or calibrated forecasts. "
         "One vote per query, divided among all tied highest-RRF candidates in the retrieved set. "
         "Histogram bins are historical GKG observation dates, not verified event dates. "
         "S&P 500 daily-close price returns are decimal fractions, T0 through T20. "
         "Shared dates and overlapping paths are dependent; a smooth mean can hide rough paths. "
         "Metadata similarity does not verify article content. Use this context or decline to use it. "
         "No mean confidence interval or prediction interval has been computed here.")

def check(ok, message):
    if not ok:
        raise ValueError(message)

def packets(pages):
    """Expose the exact numeric payloads used by the existing HTML reports."""
    parsed = {}
    for key, html in pages.items():
        matches = re.findall(rb'<script id="report-data" type="application/json">(.*?)</script>', html, re.S)
        check(len(matches) == 1, "Expected exactly one report-data payload per page.")
        parsed[key] = json.loads(matches[0])
    market = parsed[("dates_and_market", 1)]
    cases = [case for key in sorted(parsed) if key[0] == "best_matches" for case in parsed[key]["cases"]]
    check(len(cases) == market["query_documents"], "Report cases and market cohort disagree.")
    check(all(value["run_id"] == market["run_id"] for value in parsed.values()), "Mixed report runs.")
    histogram = [{k: row[k] for k in ("observed_day", "votes", "distinct_historical_documents", "anchor_date", "day20_date")}
                 for row in market["date_rows"]]
    maximum = max((row["votes"] for row in histogram), default=0)
    summary = {"schema": "gdelt-demo-evidence-v1", "interpretation": NOTES,
               **{k: market[k] for k in ("run_id", "context", "query_documents", "historical_documents",
                    "voted_queries", "total_votes", "unique_matched_articles", "unique_market_anchors")},
               "histogram_bin": "historical observed_day, UTC; counts are query-vote mass",
               "historical_hit_histogram": histogram,
               "highest_vote_dates": [row for row in histogram if row["votes"] == maximum],
               "weighted_price_return_path": market["weighted_path"], "article_page_size": 4}
    return summary, cases

ROOT = Path(__file__).resolve().parent
FILES = ("summary.json", "articles.json", "paths.json")
POLICY = {"role": "historical_context", "authority": "advisory",
          "conflict_policy": "case_by_case_evidence_weighting", "automatic_source_precedence": False,
          "trade_authority": False}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    """Atomic JSON publication; readers never see a partially written result."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n")
    temporary.replace(path)


def build_evidence(pages, source_receipt):
    summary, cases = packets(pages)
    market = json.loads(re.findall(rb'<script id="report-data" type="application/json">(.*?)</script>',
                                  pages[("dates_and_market", 1)], re.S)[0])
    check(summary["run_id"] == source_receipt["run_id"], "Source receipt and reports have different run IDs.")
    rows = market["date_rows"]
    check(len({r["observed_day"] for r in rows}) == len(rows), "Duplicate historical date.")
    check(len({c["query"]["document_id"] for c in cases}) == len(cases), "Duplicate query document.")
    for row in rows:
        path = row["forward_returns"]
        check(len(path) == 21 and all(type(v) in (int, float) and math.isfinite(v) for v in path)
                   and path[0] == 0 and row["votes"] > 0, "Invalid T0–T20 path or vote mass.")
    weight = sum(r["votes"] for r in rows)
    check(math.isclose(weight, summary["total_votes"], abs_tol=1e-8), "Histogram vote mass differs.")
    mean = summary["weighted_price_return_path"]
    check([p["horizon_sessions"] for p in mean] == list(range(21)), "Weighted path must contain T0–T20.")
    for point in mean:
        expected = sum(r["votes"] * r["forward_returns"][point["horizon_sessions"]] for r in rows) / weight
        check(math.isclose(expected, point["weighted_price_return"], abs_tol=1e-9),
                   "Individual paths disagree with the saved weighted mean beyond source rounding.")
    return {"summary": summary, "articles": {"run_id": summary["run_id"], "cases": cases},
            "paths": {"run_id": summary["run_id"], "date_rows": rows}}


def export_evidence(pages, source_receipt, folder):
    packet = build_evidence(pages, source_receipt)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "receipt.json").exists():
        saved = load_evidence(folder)
        check(saved["receipt"]["source_receipt"] == source_receipt, "Use another folder for a different source run.")
        check(all(saved[key] == packet[key] for key in ("summary", "articles", "paths")), "Saved evidence differs.")
        return saved
    check(not any(folder.iterdir()), "Incomplete or occupied export directory; preserve it and use a new folder.")
    for name in FILES:
        write(folder / name, packet[Path(name).stem])
    write(folder / "receipt.json", {"schema": "gdelt-research-evidence-v1", "run_id": packet["summary"]["run_id"],
          "complete": True, "created_at": datetime.now(timezone.utc).isoformat(), "source_receipt": source_receipt,
          "files": {name: digest((folder / name).read_bytes()) for name in FILES}})
    return load_evidence(folder)


def load_evidence(folder):
    folder = Path(folder)
    raw = (folder / "receipt.json").read_bytes()
    receipt = json.loads(raw)
    check(receipt.get("schema") == "gdelt-research-evidence-v1" and receipt.get("complete") is True,
               "A completed research-evidence receipt is required.")
    check(set(receipt["files"]) == set(FILES), "Unexpected evidence file manifest.")
    packet = {"receipt": receipt, "input_sha256": digest(raw)}
    for name in FILES:
        data = (folder / name).read_bytes()
        check(digest(data) == receipt["files"][name], "Evidence digest changed: " + name)
        value = json.loads(data)
        check(value["run_id"] == receipt["run_id"], "Mixed evidence runs: " + name)
        packet[Path(name).stem] = value
    return packet


def render(folder):
    folder = Path(folder)
    packet = load_evidence(folder)
    languages = None
    if (folder / "headline_languages.json").exists():
        languages = json.loads((folder / "headline_languages.json").read_text())
        check(languages.get("schema") == "gdelt-headline-languages-v1"
                   and languages.get("run_id") == packet["summary"]["run_id"]
                   and languages.get("input_sha256") == packet["input_sha256"]
                   and languages.get("display_only") is True,
                   "Headline-language annotations belong to different evidence; regenerate them.")
    result, research_receipt, provenance = None, None, None
    if (folder / "research_receipt.json").exists():
        research_receipt = json.loads((folder / "research_receipt.json").read_text())
        check(research_receipt["run_id"] == packet["summary"]["run_id"]
                   and research_receipt["input_sha256"] == packet["input_sha256"], "Research belongs to different evidence.")
        if research_receipt.get("complete") is True:
            check(research_receipt.get("status") == "completed", "Inconsistent research completion state.")
            data = (folder / "research.json").read_bytes()
            check(digest(data) == research_receipt["research_sha256"], "Research report digest changed.")
            result = json.loads(data)
            legacy = result.get("schema") == "gdelt-research-report-v1" and result.get("analyst_precedence") is True
            current = result.get("schema") == "gdelt-research-report-v2" and result.get("automatic_source_precedence") is False
            check((legacy or current)
                       and result.get("run_id") == packet["summary"]["run_id"]
                       and result.get("input_sha256") == packet["input_sha256"]
                       and result.get("authority") == "advisory",
                       "Research report has a different run, evidence binding or authority.")
            provenance = {"source_research_file": "research.json", "source_research_sha256": digest(data),
                "source_research_receipt": research_receipt,
                "source_receipt_sha256": digest((folder / "research_receipt.json").read_bytes()),
                "legacy_precedence_prompt": legacy, "model_rerun": False,
                "delivery_adaptation": "Original narrative unchanged. Delivery adds factual scope disclosures and removes embedded authority metadata; the handoff policy governs source weighting. Source hashes refer to the archived original files, not this delivery view."}
            result = {key: value for key, value in result.items()
                      if key not in ("authority", "analyst_precedence", "automatic_source_precedence", "disclosure")}
            result["schema"] = "gdelt-research-view-v2"
    scope = disclosure(packet, research_receipt)
    write(folder / "disclosure.json", scope)
    handoff = {"schema": "gdelt-research-handoff-v2", "run_id": packet["summary"]["run_id"],
               "delivery_created_at": datetime.now(timezone.utc).isoformat(),
               "status": "completed" if result is not None else "research_not_ready", "policy": POLICY,
               "disclosure": scope, "evidence_summary": packet["summary"], "research": result,
               "research_provenance": provenance,
               "evidence_files": packet["receipt"]["files"], "input_sha256": packet["input_sha256"]}
    write(folder / "handoff.json", handoff)
    data = {"summary": packet["summary"] | {"historical_hit_histogram": packet["paths"]["date_rows"]},
            "articles": packet["articles"], "research": result, "research_receipt": research_receipt,
            "headline_languages": languages,
            "disclosure": scope, "research_provenance": provenance,
            "synthetic": bool(packet["summary"].get("context", {}).get("synthetic")), "handoff": handoff}
    template = (ROOT / "research_view.html").read_text()
    check(template.count("__GDELT_DEMO_DATA__") == 1, "Unexpected demo template.")
    html = template.replace("__GDELT_DEMO_DATA__", json.dumps(data, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c"))
    temporary = folder / "index.html.tmp"
    temporary.write_text(html)
    temporary.replace(folder / "index.html")
    return folder / "index.html"
