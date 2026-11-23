#!/usr/bin/env python3
"""Display validated BigQuery HTML in memory; submit no SQL or write report data."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server

FIELDS = {
    "run_id": "STRING",
    "report_name": "STRING",
    "page_number": "INTEGER",
    "html_text": "STRING",
    "html_sha256": "STRING",
    "html_bytes": "INTEGER",
    "created_at": "TIMESTAMP",
}
ROUTES = {
    "/": "best_matches",
    "/best-matches": "best_matches",
    "/market": "dates_and_market",
    "/performance": "performance",
}
HEADERS = [
    ("Cache-Control", "no-store, private"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'none'; connect-src 'none'; base-uri 'none'; object-src 'none'; "
        "form-action 'none'; frame-ancestors 'none'",
    ),
]


class ReportError(ValueError):
    """A safe, actionable report-validation message."""


# Reject a report whose receipt or stored contents fail a required check.
def require(condition, message):
    if not condition:
        raise ReportError(message)


# Read the stored pages and verify that they belong to the completed report in the receipt.
def load_pages(receipt, client):
    """Only metadata and table-data reads; receipt pins one completed report."""
    for name, pattern in (
        ("project", r"[a-z][a-z0-9-]{4,61}[a-z0-9]"),
        ("dataset", r"[A-Za-z_][A-Za-z0-9_]{0,1023}"),
        ("table", r"[A-Za-z_][A-Za-z0-9_]{0,1023}"),
        ("run_id", r"[A-Za-z0-9_-]{1,128}"),
    ):
        require(
            isinstance(receipt.get(name), str) and re.fullmatch(pattern, receipt[name]),
            "Invalid report receipt identifier: " + name,
        )
    count = receipt.get("expected_rows")
    require(type(count) is int and 3 <= count <= 27, "Expected 3 through 27 report pages")
    require(
        re.fullmatch(r"[0-9]{10,16}", str(receipt.get("last_modified_time"))),
        "Missing completed report-table modification time",
    )
    table_id = ".".join(receipt[k] for k in ("project", "dataset", "table"))

    # Check the stored table's schema and identity and return its version tag.
    def metadata(table):
        schema = table.schema
        require(
            str(table.reference) == table_id and table.table_type == "TABLE",
            "Report table identity or type differs from receipt",
        )
        require(
            len(schema) == len(FIELDS)
            and {f.name: f.field_type for f in schema} == FIELDS
            and all(f.mode != "REPEATED" and not f.fields for f in schema),
            "Report table schema differs from the seven-column contract",
        )
        require(table.etag, "Incomplete report metadata")
        require(
            str(table.to_api_repr().get("lastModifiedTime")) == str(receipt["last_modified_time"])
            and table.num_rows == count,
            "Report table changed after receipt creation",
        )
        require(0 <= table.num_bytes <= 64 * 1024**2, "Report table exceeds 64 MiB read bound")
        return table.etag

    before = client.get_table(table_id, retry=None, timeout=60)
    etag = metadata(before)
    expected = {("best_matches", p) for p in range(1, count - 1)}
    expected |= {("dates_and_market", 1), ("performance", 1)}
    pages, total = {}, 0
    for row in client.list_rows(
        before, max_results=count + 1, page_size=count, retry=None, timeout=60
    ):
        key = (row["report_name"], row["page_number"])
        require(
            row["run_id"] == receipt["run_id"] and key in expected and key not in pages,
            "Unexpected, duplicate or wrong-run report page",
        )
        require(isinstance(row["html_text"], str), "Report HTML is missing")
        data = row["html_text"].encode("utf-8")
        require(
            data.startswith(b"<!doctype html>") and data.endswith(b"</html>"),
            "Report HTML is incomplete",
        )
        require(
            0 < len(data) <= 2 * 1024**2
            and len(data) == row["html_bytes"]
            and hashlib.sha256(data).hexdigest() == row["html_sha256"],
            "Report HTML size or digest differs from its saved value",
        )
        total += len(data)
        require(total <= 32 * 1024**2, "Reports exceed 32 MiB in-memory bound")
        pages[key] = data
    require(set(pages) == expected, "Report page set is incomplete")
    require(
        metadata(client.get_table(table_id, retry=None, timeout=60)) == etag,
        "Report changed during reading",
    )
    return pages


# Translate a report URL and page number into the stored page key.
def route_key(target):
    parsed = urlsplit(target)
    require(
        len(target) <= 256 and not (parsed.scheme or parsed.netloc or parsed.fragment),
        "Unsupported report route",
    )
    require(parsed.path in ROUTES, "Unknown report route")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
    require(
        set(query) <= {"page"} and all(len(v) == 1 for v in query.values()),
        "Unsupported report parameter",
    )
    page = query.get("page", ["1"])[0]
    require(re.fullmatch(r"[1-9][0-9]?", page), "Invalid report page")
    return ROUTES[parsed.path], int(page)


# Create a web application that serves the verified report pages.
def application(pages):
    # Return a stored page or an HTTP error for an unsupported method or route.
    def serve(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path, query = environ.get("PATH_INFO", "/"), environ.get("QUERY_STRING", "")
        status, data, mime = "200 OK", b"ok\n", "text/plain; charset=utf-8"
        headers = list(HEADERS)
        if method not in ("GET", "HEAD"):
            status, data = "405 Method Not Allowed", b"Method not allowed\n"
            headers.append(("Allow", "GET, HEAD"))
        elif path != "/healthz" or query:
            try:
                data = pages[route_key(path + ("?" + query if query else ""))]
                mime = "text/html; charset=utf-8"
            except (ValueError, KeyError):
                status, data = "404 Not Found", b"Unknown report page\n"
        start_response(
            status, [("Content-Type", mime), ("Content-Length", str(len(data))), *headers]
        )
        return [] if method == "HEAD" else [data]

    return serve


# Construct the report viewer from its receipt and the corresponding BigQuery pages.
def create_app(receipt_path=None):
    """WSGI factory; Cloud Run must use private IAM/IAP, as with the prior service."""
    require(
        os.environ.get("K_SERVICE")
        or re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z0-9.-]+", os.environ.get("WEB_HOST", "")),
        "Run the viewer in Cloud Shell or a private Cloud Run service",
    )
    try:
        path = Path(receipt_path or os.environ.get("GDELT_REPORT_RECEIPT", "report_receipt.json"))
        require(path.stat().st_size <= 16384, "Report receipt exceeds 16 KiB")
        from google.cloud import bigquery

        receipt = json.loads(path.read_text(encoding="utf-8"))
        with bigquery.Client(project=receipt["project"]) as client:
            pages = load_pages(receipt, client)
    except ReportError:
        raise
    except Exception:
        raise ReportError("Report loading failed; check receipt and table access") from None
    return application(pages)


# Run the report viewer or check that its saved pages are available.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", default="report_receipt.json")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    try:
        require(2000 <= args.port <= 65000, "Preview port must be 2000 through 65000")
        app = create_app(args.receipt)

        class Quiet(WSGIRequestHandler):
            # Suppress routine HTTP access messages from the local report server.
            def log_message(self, *args):
                pass

        with make_server("0.0.0.0", args.port, app, handler_class=Quiet) as server:
            host = os.environ.get("WEB_HOST")
            print(
                f"Reports ready: https://{args.port}-{host}/" if host else "Reports ready",
                flush=True,
            )
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ReportError)
            else "Report loading failed; check receipt and table access"
        )
        print(json.dumps({"state": "STOPPED", "message": message}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
