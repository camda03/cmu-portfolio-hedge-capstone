"""Connect fresh GKG intake to the existing BigQuery matcher and evidence reports."""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parent
NOTES = (
    "Historical retrieval evidence, not event probabilities or calibrated forecasts. "
    "One vote per query, divided among all tied highest-RRF candidates in the retrieved set. "
    "Histogram bins are historical GKG observation dates, not verified event dates. "
    "S&P 500 daily-close price returns are decimal fractions, T0 through T20. "
    "Shared dates and overlapping paths are dependent; a smooth mean can hide rough paths. "
    "Metadata similarity does not verify article content. Use this context or decline to use it. "
    "No mean confidence interval or prediction interval has been computed here."
)


# Stop the batch when a required input or result fails its check.
def check(ok, message):
    if not ok:
        raise ValueError(message)


# Identify the exact driver and SQL files used by the GDELT batch.
def fingerprint():
    paths = [Path(__file__), *sorted((ROOT / "demo_sql").glob("*.sql"))]
    return hashlib.sha256(b"\n".join(p.read_bytes() for p in paths)).hexdigest()


# Choose a complete UTC news hour or validate the hour supplied by the caller.
def closed_hour(value=None):
    now = datetime.now(timezone.utc)
    start = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value
        else (now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2))  # Leave a full hour for ingestion.
    )
    check(start.tzinfo is not None, "The news-window start needs an explicit UTC offset.")
    start = start.astimezone(timezone.utc)
    check(start + timedelta(hours=1) <= now, "The news hour must already be closed.")
    return start.isoformat()


# Register the demo's selection, embedding, and publication steps with the runner.
def stages(runner):
    # Names, statement ceilings and scalar result schemas use the existing runner.
    for name, statements, fields, params in [
        ("smoke", 3, {"status": "STRING", "dimensions": "INT64"}, {}),
        ("cache", 4, {}, {"expected_query_documents": "INT64"}),
        ("counts", 1, {"new_inputs": "INT64", "cached_inputs": "INT64"}, {}),
        ("embed", 2, {}, {"new_inputs": "INT64"}),
        ("publish", 4, {}, {"expected_query_documents": "INT64"}),
    ]:
        runner.stages["demo_" + name] = {
            "sql": "demo_sql/" + name + ".sql",
            "maximum_bytes_billed": 100_000_000_000 if name == "cache" else 10_000_000_000,
            "statement_count": statements,
            "parameters": params,
            "kind": "embedding" if name in ("embed", "smoke") else "derived",
            "max_result_rows": int(bool(fields)),
            "fields": fields,
        }


# Use the existing index to retrieve analogs and publish reports for a news hour.
def run_batch(args):
    import run as engine  # Reuse durable job IDs, budgets, retrieval SQL and report calculations.

    build, path = args.build_state.resolve(), args.state.resolve()
    check(build != path, "Build and demo state files must differ.")
    with ExitStack() as stack:
        for item in (build, path):
            lock = stack.enter_context(item.with_suffix(item.suffix + ".lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent = engine.Run(build)
        check(
            parent.config["mode"] == "filtered"
            and parent.state.get("built")
            and parent.state.get("complete"),
            "Complete and verify the two-million filtered build first.",
        )
        if not path.exists():
            engine.prepare(path, "filtered")
            saved = engine.read(path)
            launch = {
                "config": saved["config"],
                "build_state": str(build),
                "window_start": closed_hour(args.window_start),
                "raw_limit": args.raw_limit or 6000,
                "parent_config_sha256": parent.state["config_sha256"],
                "demo_code_sha256": fingerprint(),
            }
            engine.save(path, {"launch": launch})  # Freeze the hour even if intake is interrupted.
        saved = engine.read(path)
        frozen = saved.get("launch", saved.get("config", {}))
        check(
            frozen.get("build_state") == str(build)
            and frozen.get("parent_config_sha256") == parent.state["config_sha256"],
            "This demo is bound to a different build. Keep its state; use a new filename for another run.",
        )
        check(
            frozen.get("demo_code_sha256") == fingerprint(),
            "Demo code changed; resume with its original code.",
        )
        window = frozen.get("window_start") or frozen["parameters"]["window_start"]
        raw_limit = frozen.get("raw_limit") or frozen["intake"]["raw_limit"]
        check(
            not args.window_start or closed_hour(args.window_start) == window,
            "The saved news window cannot change.",
        )
        check(
            not args.raw_limit or args.raw_limit == raw_limit,
            "The saved sample limit cannot change.",
        )
        if "launch" in saved:
            parent.stages["live/select_500_qualified"]["sql"] = "demo_sql/select.sql"
            parent.stages["live/request_candidates"]["sql"] = "demo_sql/requests.sql"
            parent.intake(window, raw_limit)
            batch = "b_" + engine.digest([window, raw_limit])[:12]
            counts = parent.state["jobs"][batch + "/live/selection_status"]["rows"][0]
            config = deepcopy(parent.config)
            config.update(
                {k: frozen["config"][k] for k in ("dataset", "run_id", "created_at", "code_sha256")}
            )
            config.update(
                purpose="fresh_gkg_demo",
                build_state=str(build),
                demo_code_sha256=fingerprint(),
                parent_config_sha256=frozen["parent_config_sha256"],
                intake=parent.state["intake_windows"][batch]
                | {"counts": counts, "batch_id": batch},
            )
            source = parent.config["dataset"] + ".filtered_live_" + batch
            config["identifiers"].update(
                dataset=config["dataset"],
                query_documents=source + "_query_documents_v01",
                intake_requests=source + "_request_candidates_v01",
                query_requests=config["dataset"] + ".demo_requests_v01",
                query_vectors=config["dataset"] + ".demo_vectors_v01",
            )
            config["parameters"].update(
                run_id=config["run_id"],
                window_start=window,
                as_of_utc=config["intake"]["as_of_utc"],
                expected_query_documents=min(500, counts["expanded_fresh_documents"]),
                expected_query_inputs=None,
            )
            engine.save(
                path,
                {
                    "config": config,
                    "config_sha256": engine.digest(config),
                    "jobs": {},
                    "plans": {},
                    "built": True,
                    "note": "Verified parent history is reused, not rebuilt.",
                },
            )
        demo = engine.Run(path)
        stages(demo)
        demo.destination()
        demo.step("demo_cache")
        counts = demo.step("demo_counts")[0]
        check(
            0 <= counts["new_inputs"] <= 500, "More than 500 new query inputs; nothing submitted."
        )
        demo.step(
            "demo_embed", extra={"new_inputs": counts["new_inputs"]}, inputs=counts["new_inputs"]
        )
        demo.step("demo_publish")
        demo.evaluate()


# Run the small BigQuery check that exercises the demo's retrieval setup.
def smoke(args):
    import run as engine

    path = args.state.resolve()
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not path.exists():
            engine.prepare(path, "saved")
        runner = engine.Run(path)
        stages(runner)
        print(
            engine.encode(
                {"state": "MODEL_SMOKE_PASS", "result": runner.step("demo_smoke", inputs=1)}
            )
        )


# Extract the summary and article pairs embedded in the stored report pages.
def packets(pages):
    """Expose the exact numeric payloads used by the existing HTML reports."""
    parsed = {}
    for key, html in pages.items():
        matches = re.findall(
            rb'<script id="report-data" type="application/json">(.*?)</script>', html, re.S
        )
        check(len(matches) == 1, "Expected exactly one report-data payload per page.")
        parsed[key] = json.loads(matches[0])
    market = parsed[("dates_and_market", 1)]
    cases = [
        case for key in sorted(parsed) if key[0] == "best_matches" for case in parsed[key]["cases"]
    ]
    check(len(cases) == market["query_documents"], "Report cases and market cohort disagree.")
    check(
        all(value["run_id"] == market["run_id"] for value in parsed.values()), "Mixed report runs."
    )
    histogram = [
        {
            k: row[k]
            for k in (
                "observed_day",
                "votes",
                "distinct_historical_documents",
                "anchor_date",
                "day20_date",
            )
        }
        for row in market["date_rows"]
    ]
    maximum = max((row["votes"] for row in histogram), default=0)
    summary = {
        "schema": "gdelt-demo-evidence-v1",
        "interpretation": NOTES,
        **{
            k: market[k]
            for k in (
                "run_id",
                "context",
                "query_documents",
                "historical_documents",
                "voted_queries",
                "total_votes",
                "unique_matched_articles",
                "unique_market_anchors",
            )
        },
        "histogram_bin": "historical observed_day, UTC; counts are query-vote mass",
        "historical_hit_histogram": histogram,
        "highest_vote_dates": [row for row in histogram if row["votes"] == maximum],
        "weighted_price_return_path": market["weighted_path"],
        "article_page_size": 4,
    }
    return summary, cases


# Add an evidence endpoint to the existing report viewer.
def evidence_app(pages, base, headers):
    summary, cases = packets(pages)

    # Return the requested evidence selection or pass the request to the report viewer.
    def serve(environ, start_response):
        if environ.get("PATH_INFO") != "/evidence":
            return base(environ, start_response)
        try:
            check(environ.get("REQUEST_METHOD", "GET") in ("GET", "HEAD"), "Read-only endpoint.")
            query = parse_qs(
                environ.get("QUERY_STRING", ""),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=1,
            )
            check(not query or set(query) == {"page"}, "Use /evidence or /evidence?page=N.")
            payload = summary
            if query:
                value = query["page"]
                check(
                    len(value) == 1 and re.fullmatch(r"[1-9][0-9]{0,2}", value[0]), "Invalid page."
                )
                first = (int(value[0]) - 1) * 4
                check(first < len(cases), "Unknown page.")
                payload = {
                    "schema": summary["schema"],
                    "run_id": summary["run_id"],
                    "interpretation": NOTES,
                    "total_cases": len(cases),
                    "page": int(value[0]),
                    "cases": cases[first : first + 4],
                }
            data, status = (
                json.dumps(payload, allow_nan=False, separators=(",", ":")).encode(),
                "200 OK",
            )
        except (ValueError, KeyError):
            data, status = b'{"error":"Invalid read-only evidence request"}', "400 Bad Request"
        start_response(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(data))),
                *headers,
            ],
        )
        return [] if environ.get("REQUEST_METHOD") == "HEAD" else [data]

    return serve


# Check the published pages and export or serve the corresponding reports.
def outputs(args):
    import view_reports as viewer
    from google.cloud import bigquery

    check(args.receipt.stat().st_size <= 16384, "Oversized report receipt.")
    receipt = json.loads(args.receipt.read_text())
    with bigquery.Client(project=receipt["project"]) as client:
        pages = viewer.load_pages(receipt, client)
    summary, cases = packets(pages)
    if args.command == "export":
        import run as engine

        args.output.mkdir(parents=True, exist_ok=True)
        check(
            not any(args.output.iterdir()),
            "Export directory must be empty; use a new name, preserving the old snapshot.",
        )
        engine.save(args.output / "summary.json", summary)
        engine.save(args.output / "articles.json", {"run_id": summary["run_id"], "cases": cases})
        engine.save(
            args.output / "receipt.json", receipt
        )  # Written last; absent means an incomplete export.
        print(
            json.dumps(
                {"state": "EXPORTED", "output": str(args.output), "run_id": summary["run_id"]}
            )
        )
        return
    check(
        os.environ.get("K_SERVICE")
        or re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z0-9.-]+", os.environ.get("WEB_HOST", "")),
        "Use authenticated Cloud Shell preview or a private Cloud Run service.",
    )
    check(2000 <= args.port <= 65000, "Invalid preview port.")
    if summary["context"].get("purpose") == "fresh_gkg_demo":
        pages = {
            key: html.replace(b"saved-cohort evaluation", b"fresh-window evaluation")
            for key, html in pages.items()
        }
    app = evidence_app(pages, viewer.application(pages), viewer.HEADERS)
    with make_server("0.0.0.0", args.port, app) as server:
        print("Reports and /evidence ready on port " + str(args.port), flush=True)
        server.serve_forever()


# Read the command line and run the selected GDELT batch or reporting operation.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "smoke", "serve", "export"])
    parser.add_argument("--build-state", type=Path, default=Path("filtered_state.json"))
    parser.add_argument("--state", type=Path, default=Path("demo_state.json"))
    parser.add_argument("--window-start")
    parser.add_argument("--raw-limit", type=int, choices=[6000, 12000, 24000])
    parser.add_argument("--receipt", type=Path, default=Path("demo_state_reports.json"))
    parser.add_argument("--output", type=Path, default=Path("demo_output"))
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.command == "run":
        run_batch(args)
    elif args.command == "smoke":
        smoke(args)
    else:
        outputs(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(
            json.dumps(
                {
                    "state": "STOPPED",
                    "error": str(error),
                    "resume": "Keep all state files and the same code.",
                }
            ),
            flush=True,
        )
        raise SystemExit(1)
