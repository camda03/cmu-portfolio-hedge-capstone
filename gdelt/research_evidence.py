"""Reuse completed BigQuery reports for a research packet and a standalone demo."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from wsgiref.simple_server import make_server

import demo
import view_reports
from research_disclosure import disclosure

ROOT = Path(__file__).resolve().parent
FILES = ("summary.json", "articles.json", "paths.json")
POLICY = {
    "role": "historical_context",
    "authority": "advisory",
    "conflict_policy": "case_by_case_evidence_weighting",
    "automatic_source_precedence": False,
    "trade_authority": False,
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    """Atomic JSON publication; readers never see a partially written result."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n")
    temporary.replace(path)


def build_evidence(pages, source_receipt):
    summary, cases = demo.packets(pages)
    market = json.loads(
        re.findall(
            rb'<script id="report-data" type="application/json">(.*?)</script>',
            pages[("dates_and_market", 1)],
            re.S,
        )[0]
    )
    demo.check(
        summary["run_id"] == source_receipt["run_id"],
        "Source receipt and reports have different run IDs.",
    )
    rows = market["date_rows"]
    demo.check(len({r["observed_day"] for r in rows}) == len(rows), "Duplicate historical date.")
    demo.check(
        len({c["query"]["document_id"] for c in cases}) == len(cases), "Duplicate query document."
    )
    for row in rows:
        path = row["forward_returns"]
        demo.check(
            len(path) == 21
            and all(type(v) in (int, float) and math.isfinite(v) for v in path)
            and path[0] == 0
            and row["votes"] > 0,
            "Invalid T0–T20 path or vote mass.",
        )
    weight = sum(r["votes"] for r in rows)
    demo.check(
        math.isclose(weight, summary["total_votes"], abs_tol=1e-8), "Histogram vote mass differs."
    )
    mean = summary["weighted_price_return_path"]
    demo.check(
        [p["horizon_sessions"] for p in mean] == list(range(21)),
        "Weighted path must contain T0–T20.",
    )
    for point in mean:
        expected = (
            sum(r["votes"] * r["forward_returns"][point["horizon_sessions"]] for r in rows) / weight
        )
        demo.check(
            math.isclose(expected, point["weighted_price_return"], abs_tol=1e-9),
            "Individual paths disagree with the saved weighted mean beyond source rounding.",
        )
    return {
        "summary": summary,
        "articles": {"run_id": summary["run_id"], "cases": cases},
        "paths": {"run_id": summary["run_id"], "date_rows": rows},
    }


def export_evidence(pages, source_receipt, folder):
    packet = build_evidence(pages, source_receipt)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "receipt.json").exists():
        saved = load_evidence(folder)
        demo.check(
            saved["receipt"]["source_receipt"] == source_receipt,
            "Use another folder for a different source run.",
        )
        demo.check(
            all(saved[key] == packet[key] for key in ("summary", "articles", "paths")),
            "Saved evidence differs.",
        )
        return saved
    demo.check(
        not any(folder.iterdir()),
        "Incomplete or occupied export directory; preserve it and use a new folder.",
    )
    for name in FILES:
        write(folder / name, packet[Path(name).stem])
    write(
        folder / "receipt.json",
        {
            "schema": "gdelt-research-evidence-v1",
            "run_id": packet["summary"]["run_id"],
            "complete": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_receipt": source_receipt,
            "files": {name: digest((folder / name).read_bytes()) for name in FILES},
        },
    )
    return load_evidence(folder)


def load_evidence(folder):
    folder = Path(folder)
    raw = (folder / "receipt.json").read_bytes()
    receipt = json.loads(raw)
    demo.check(
        receipt.get("schema") == "gdelt-research-evidence-v1" and receipt.get("complete") is True,
        "A completed research-evidence receipt is required.",
    )
    demo.check(set(receipt["files"]) == set(FILES), "Unexpected evidence file manifest.")
    packet = {"receipt": receipt, "input_sha256": digest(raw)}
    for name in FILES:
        data = (folder / name).read_bytes()
        demo.check(digest(data) == receipt["files"][name], "Evidence digest changed: " + name)
        value = json.loads(data)
        demo.check(value["run_id"] == receipt["run_id"], "Mixed evidence runs: " + name)
        packet[Path(name).stem] = value
    return packet


def render(folder):
    folder = Path(folder)
    packet = load_evidence(folder)
    languages = None
    if (folder / "headline_languages.json").exists():
        languages = json.loads((folder / "headline_languages.json").read_text())
        demo.check(
            languages.get("schema") == "gdelt-headline-languages-v1"
            and languages.get("run_id") == packet["summary"]["run_id"]
            and languages.get("input_sha256") == packet["input_sha256"]
            and languages.get("display_only") is True,
            "Headline-language annotations belong to different evidence; regenerate them.",
        )
    result, research_receipt, provenance = None, None, None
    if (folder / "research_receipt.json").exists():
        research_receipt = json.loads((folder / "research_receipt.json").read_text())
        demo.check(
            research_receipt["run_id"] == packet["summary"]["run_id"]
            and research_receipt["input_sha256"] == packet["input_sha256"],
            "Research belongs to different evidence.",
        )
        if research_receipt.get("complete") is True:
            demo.check(
                research_receipt.get("status") == "completed",
                "Inconsistent research completion state.",
            )
            data = (folder / "research.json").read_bytes()
            demo.check(
                digest(data) == research_receipt["research_sha256"],
                "Research report digest changed.",
            )
            result = json.loads(data)
            legacy = (
                result.get("schema") == "gdelt-research-report-v1"
                and result.get("analyst_precedence") is True
            )
            current = (
                result.get("schema") == "gdelt-research-report-v2"
                and result.get("automatic_source_precedence") is False
            )
            demo.check(
                (legacy or current)
                and result.get("run_id") == packet["summary"]["run_id"]
                and result.get("input_sha256") == packet["input_sha256"]
                and result.get("authority") == "advisory",
                "Research report has a different run, evidence binding or authority.",
            )
            provenance = {
                "source_research_file": "research.json",
                "source_research_sha256": digest(data),
                "source_research_receipt": research_receipt,
                "source_receipt_sha256": digest((folder / "research_receipt.json").read_bytes()),
                "legacy_precedence_prompt": legacy,
                "model_rerun": False,
                "delivery_adaptation": "Original narrative unchanged. Delivery adds factual scope disclosures and removes embedded authority metadata; the handoff policy governs source weighting. Source hashes refer to the archived original files, not this delivery view.",
            }
            result = {
                key: value
                for key, value in result.items()
                if key
                not in (
                    "authority",
                    "analyst_precedence",
                    "automatic_source_precedence",
                    "disclosure",
                )
            }
            result["schema"] = "gdelt-research-view-v2"
    scope = disclosure(packet, research_receipt)
    write(folder / "disclosure.json", scope)
    handoff = {
        "schema": "gdelt-research-handoff-v2",
        "run_id": packet["summary"]["run_id"],
        "delivery_created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if result is not None else "research_not_ready",
        "policy": POLICY,
        "disclosure": scope,
        "evidence_summary": packet["summary"],
        "research": result,
        "research_provenance": provenance,
        "evidence_files": packet["receipt"]["files"],
        "input_sha256": packet["input_sha256"],
    }
    write(folder / "handoff.json", handoff)
    data = {
        "summary": packet["summary"] | {"historical_hit_histogram": packet["paths"]["date_rows"]},
        "articles": packet["articles"],
        "research": result,
        "research_receipt": research_receipt,
        "headline_languages": languages,
        "disclosure": scope,
        "research_provenance": provenance,
        "synthetic": bool(packet["summary"].get("context", {}).get("synthetic")),
        "handoff": handoff,
    }
    template = (ROOT / "research_demo.html").read_text()
    demo.check(template.count("__GDELT_DEMO_DATA__") == 1, "Unexpected demo template.")
    html = template.replace(
        "__GDELT_DEMO_DATA__",
        json.dumps(data, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c"),
    )
    temporary = folder / "index.html.tmp"
    temporary.write_text(html)
    temporary.replace(folder / "index.html")
    return folder / "index.html"


def refresh(source, folder):
    """Create a new delivery snapshot; original evidence, narrative and receipts stay byte-identical."""
    source, folder = Path(source).resolve(), Path(folder).resolve()
    demo.check(
        source != folder, "Refresh into a separate directory to preserve the previous delivery."
    )
    packet = load_evidence(source)
    folder.mkdir(parents=True, exist_ok=True)
    for name in (*FILES, "receipt.json", "research.json", "research_receipt.json"):
        original, target = source / name, folder / name
        if original.exists():
            demo.check(
                not target.exists() or target.read_bytes() == original.read_bytes(),
                "Existing snapshot differs; use another output directory: " + name,
            )
            if not target.exists():
                shutil.copyfile(original, target)
        else:
            demo.check(
                not target.exists(), "A previous report must not leak into a pending delivery."
            )
    demo.check(
        load_evidence(folder)["input_sha256"] == packet["input_sha256"], "Copied evidence differs."
    )
    return render(folder)


def serve(folder, port):
    folder = Path(folder)
    render(folder)
    allowed = (
        "index.html",
        "handoff.json",
        "disclosure.json",
        "headline_languages.json",
        *FILES,
        "receipt.json",
    )

    def application(environ, start_response):
        name = environ.get("PATH_INFO", "/").removeprefix("/") or "index.html"
        ok = (
            name in allowed
            and (folder / name).is_file()
            and environ.get("REQUEST_METHOD") in ("GET", "HEAD")
        )
        data = (folder / name).read_bytes() if ok else b"Not found"
        mime = "text/html; charset=utf-8" if name == "index.html" else "application/json"
        start_response(
            "200 OK" if ok else "404 Not Found",
            [("Content-Type", mime), ("Content-Length", str(len(data))), *view_reports.HEADERS],
        )
        return [] if environ.get("REQUEST_METHOD") == "HEAD" else [data]

    with make_server("127.0.0.1", port, application) as server:
        print(json.dumps({"state": "SERVING", "url": "http://127.0.0.1:" + str(port)}), flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "render", "serve", "refresh"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--source", type=Path, help="Existing output packet for a render-only disclosure refresh."
    )
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.command == "refresh":
        demo.check(args.source is not None, "Refresh requires --source.")
        print(
            json.dumps(
                {
                    "state": "DISCLOSURE_READY",
                    "page": str(refresh(args.source, args.output)),
                    "new_model_calls": 0,
                    "new_queries": 0,
                }
            )
        )
        return
    if args.command == "export":
        from google.cloud import bigquery

        demo.check(args.receipt is not None, "Export requires a completed report --receipt.")
        receipt = json.loads(args.receipt.read_text())
        with bigquery.Client(project=receipt["project"]) as client:
            export_evidence(view_reports.load_pages(receipt, client), receipt, args.output)
    if args.command == "serve":
        serve(args.output, args.port)
    else:
        print(json.dumps({"state": "RENDERED", "page": str(render(args.output))}), flush=True)


if __name__ == "__main__":
    main()
