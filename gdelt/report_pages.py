"""Read a completed set of BigQuery report pages and verify it against its receipt."""

import hashlib
import re

FIELDS = {"run_id": "STRING", "report_name": "STRING", "page_number": "INTEGER",
          "html_text": "STRING", "html_sha256": "STRING", "html_bytes": "INTEGER",
          "created_at": "TIMESTAMP"}

class ReportError(ValueError):
    """A safe, actionable report-validation message."""

def require(condition, message):
    if not condition:
        raise ReportError(message)

def load_pages(receipt, client):
    """Only metadata and table-data reads; receipt pins one completed report."""
    for name, pattern in (("project", r"[a-z][a-z0-9-]{4,61}[a-z0-9]"),
                          ("dataset", r"[A-Za-z_][A-Za-z0-9_]{0,1023}"),
                          ("table", r"[A-Za-z_][A-Za-z0-9_]{0,1023}"),
                          ("run_id", r"[A-Za-z0-9_-]{1,128}")):
        require(isinstance(receipt.get(name), str) and re.fullmatch(pattern, receipt[name]),
                "Invalid report receipt identifier: " + name)
    count = receipt.get("expected_rows")
    require(type(count) is int and 3 <= count <= 27, "Expected 3 through 27 report pages")
    require(re.fullmatch(r"[0-9]{10,16}", str(receipt.get("last_modified_time"))),
            "Missing completed report-table modification time")
    table_id = ".".join(receipt[k] for k in ("project", "dataset", "table"))

    def metadata(table):
        schema = table.schema
        require(str(table.reference) == table_id and table.table_type == "TABLE",
                "Report table identity or type differs from receipt")
        require(len(schema) == len(FIELDS) and {f.name: f.field_type for f in schema} == FIELDS
                and all(f.mode != "REPEATED" and not f.fields for f in schema),
                "Report table schema differs from the seven-column contract")
        require(table.etag, "Incomplete report metadata")
        require(str(table.to_api_repr().get("lastModifiedTime")) == str(receipt["last_modified_time"])
                and table.num_rows == count, "Report table changed after receipt creation")
        require(0 <= table.num_bytes <= 64 * 1024**2, "Report table exceeds 64 MiB read bound")
        return table.etag

    before = client.get_table(table_id, retry=None, timeout=60)
    etag = metadata(before)
    expected = {("best_matches", p) for p in range(1, count - 1)}
    expected |= {("dates_and_market", 1), ("performance", 1)}
    pages, total = {}, 0
    for row in client.list_rows(before, max_results=count + 1, page_size=count, retry=None, timeout=60):
        key = (row["report_name"], row["page_number"])
        require(row["run_id"] == receipt["run_id"] and key in expected and key not in pages,
                "Unexpected, duplicate or wrong-run report page")
        require(isinstance(row["html_text"], str), "Report HTML is missing")
        data = row["html_text"].encode("utf-8")
        require(data.startswith(b"<!doctype html>") and data.endswith(b"</html>"),
                "Report HTML is incomplete")
        require(0 < len(data) <= 2 * 1024**2 and len(data) == row["html_bytes"]
                and hashlib.sha256(data).hexdigest() == row["html_sha256"],
                "Report HTML size or digest differs from its saved value")
        total += len(data)
        require(total <= 32 * 1024**2, "Reports exceed 32 MiB in-memory bound")
        pages[key] = data
    require(set(pages) == expected, "Report page set is incomplete")
    require(metadata(client.get_table(table_id, retry=None, timeout=60)) == etag, "Report changed during reading")
    return pages
