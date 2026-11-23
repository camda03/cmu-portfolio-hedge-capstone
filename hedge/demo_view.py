"""Render a portable desk from one saved decision. Browser input never reaches a trading API."""

import argparse
from base64 import b64encode
from collections import defaultdict
from itertools import groupby
import json
import os
from pathlib import Path

from ledger import instant, statement
from source_dates import source_dates
from hedge_session import ROOT, read_json, require, save


# Assemble portfolio values and past decisions for the desk's performance display.
def tracker(seed, events):
    points = []

    # Select the dated values needed for one point on the portfolio chart.
    def point(book):
        return {
            k: book[k]
            for k in ("as_of", "portfolio_value_usd", "unhedged_value_usd", "hedge_after_fees_usd")
        }

    book = statement(seed, [])
    points.append(point(book))
    # Render exact recent snapshots; the complete journal remains in SQLite.
    groups = [list(group) for _, group in groupby(events, key=lambda e: instant(e["at"]))]
    prefix = [e for group in groups[:-40] for e in group]
    for group in groups[-40:]:
        prefix.extend(group)
        book = statement(seed, prefix)
        points.append(point(book))
    decisions = [
        {
            k: e.get(k)
            for k in ("at", "kind", "contract", "contracts", "fee_usd", "receipt", "reason")
        }
        for e in events
        if e["kind"] in ("adjust", "hold", "defer")
    ]
    return dict(current=book, points=points, decisions=decisions[-40:])


# Prepare the study link, evidence, and observation dates for the hedge desk.
def gdelt_view(folder, handoff):
    from hashlib import sha256

    # Verify the published checksum before reading a GDELT evidence file.
    def evidence(name):
        raw = (Path(folder) / name).read_bytes()
        require(
            sha256(raw).hexdigest() == handoff["evidence_files"][name],
            f"Chart evidence changed in {name}.",
        )
        return json.loads(raw)

    rows = evidence("paths.json")["date_rows"]
    articles = evidence("articles.json")
    cases = articles["cases"]
    by_date = defaultdict(list)
    for item in cases:
        hit = item.get("fused") or {}
        by_date[hit.get("observed_day")].append(item)
    research = handoff["research"]
    notes = {a["date"]: a for a in research.get("analogues", [])}
    dates = []
    for r in sorted(rows, key=lambda v: (-v["votes"], v["observed_day"])):
        day = r["observed_day"]
        pair = next(iter(by_date[day]), {})
        # Representatives displayed by retrieval may differ from a pair cited by the researcher.
        dates.append(
            dict(
                day=day,
                votes=r["votes"],
                anchor=r.get("anchor_date"),
                end=r.get("day20_date"),
                path=r["forward_returns"],
                query=(pair.get("query") or {}).get("title"),
                match=(pair.get("fused") or {}).get("title"),
                note=notes.get(day),
            )
        )
    s = handoff["evidence_summary"]
    template = ROOT / "demo_assets" / "gdelt-template.html"
    producer_page = None
    if template.exists():
        data = dict(
            summary=s | {"historical_hit_histogram": rows},
            articles=articles,
            research=research,
            research_receipt=(handoff.get("research_provenance") or {}).get(
                "source_research_receipt"
            ),
            disclosure=handoff["disclosure"],
            research_provenance=handoff.get("research_provenance"),
            synthetic=bool(s.get("context", {}).get("synthetic")),
            headline_languages=None,
            handoff=handoff,
        )
        producer_page = template.read_text().replace(
            "__GDELT_DEMO_DATA__", json.dumps(data, ensure_ascii=True).replace("<", "\\u003c")
        )
    return dict(
        producer_page=producer_page,
        run=handoff["run_id"],
        window=handoff["disclosure"]["observation_window"],
        synthetic=bool(s.get("context", {}).get("synthetic")),
        queries=s["query_documents"],
        corpus=s["historical_documents"],
        weight=s["total_votes"],
        dates=dates,
        mean=[v["weighted_price_return"] for v in s["weighted_price_return_path"]],
        headline=research.get("headline"),
        assessment=research["overall_assessment"],
        limitations=handoff["disclosure"]["limitations"],
        model_review=handoff["disclosure"]["model_review"],
    )


# Collect the saved decision artifacts and write the portfolio desk page.
def render(folder, gdelt_dir=None):
    payload = read_json(folder / "request.json")
    if gdelt_dir is not None:
        save(folder / "gdelt-view.json", gdelt_view(gdelt_dir, payload["gdelt"]))
    research = read_json(folder / "gdelt-view.json")
    subjects = []
    for symbol, item in sorted(payload["analysts"].items()):
        f = item["forecast"]
        subjects.append(
            dict(
                symbol=symbol,
                forecast=f,
                report=item["report_markdown"],
                sha=item["report_sha256"],
                key=item["report_key"],
                mark=payload["market"]["quotes"][symbol],
            )
        )
    journal = (
        read_json(folder / "decision-history.json")
        if (folder / "decision-history.json").exists()
        else {}
    )
    view = dict(
        as_of=payload["as_of"],
        links=payload.get("links", {}),
        report_contact=os.environ.get("PORTFOLIO_REPORT_CONTACT", "the portfolio manager"),
        batch_date=payload["batch_date"],
        analyst_run=payload["analyst_run"],
        analyst_publication=payload.get("analyst_publication"),
        revision=payload["analyst_revision"],
        packet_key=payload["packet_key"],
        hashes=payload["input_sha256"],
        analysts=subjects,
        gdelt=research,
        market=payload["market"],
        policy=payload["policy"],
        table=payload["alternatives"],
        decision=read_json(folder / "decision.json"),
        tracker=read_json(folder / "tracker.json"),
        dashboard=payload.get("dashboard_url"),
        gdelt_url=payload["policy"].get("gdelt_demo_url"),
        gdelt_gap=payload.get("gdelt_gap"),
        freshness=payload.get("source_freshness") or source_dates(payload),
        history=journal.get("decisions", []),
        history_as_of=journal.get("as_of"),
        **{
            k: payload[k]
            for k in ("snapshot_note", "snapshot_asof_label", "original_url")
            if k in payload
        },
    )
    (folder / "index.html").write_text(render_page(view))


# Fill the desk template with its scripts, styles, and serialized decision data.
def render_page(view):
    assets = ROOT / "demo_assets"
    raw = json.dumps(view, ensure_ascii=True, allow_nan=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    return (
        (assets / "desk.html")
        .read_text()
        .replace("@@STYLE@@", (assets / "desk.css").read_text())
        .replace("@@OVERVIEW@@", (assets / "overview.js").read_text())
        .replace("@@EVIDENCE@@", (assets / "evidence.js").read_text())
        .replace("@@MAP@@", b64encode((assets / "architecture.svg").read_bytes()).decode())
        .replace("@@RUN@@", raw)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    render(args.folder)
