"""Submit and resume the BigQuery stages that build and evaluate the historical news index."""

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parent
# Broad runaway guards, not per-stage or cumulative spending budgets.
PER_QUERY = 1_000_000_000_000  # 1 TB per query job/statement; not a dollar cap.
JOB_TIMEOUT_MS = 3_600_000  # BigQuery attempts cancellation after one hour.
RESERVATION = os.environ.get("GDELT_RESERVATION")


# Serialize run settings consistently so equivalent records produce the same hash.
def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# Hash a run record using its canonical JSON representation.
def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


# Load a saved run or configuration record from JSON.
def read(path):
    return json.loads(Path(path).read_text())


# Replace the stored run state only after the new JSON is complete.
def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encode(value) + "\n")
    temporary.replace(path)


# Return the current UTC timestamp for a run receipt.
def now():
    return datetime.now(timezone.utc).isoformat()


# Extract queue time, execution time, and resource usage from a BigQuery job.
def measured_job(key, job_id, statistics):
    query = statistics.get("query", {})
    # Convert the elapsed time between two BigQuery millisecond timestamps to seconds.
    seconds = lambda a, b: (
        (int(statistics[b]) - int(statistics[a])) / 1000
        if a in statistics and b in statistics
        else None
    )
    return {
        "key": key,
        "job_id": job_id,
        "queue_seconds": seconds("creationTime", "startTime"),
        "execution_seconds": seconds("startTime", "endTime"),
        "statement_type": query.get("statementType"),
        "total_slot_ms": int(query["totalSlotMs"]) if "totalSlotMs" in query else None,
        "total_bytes_billed": int(query["totalBytesBilled"])
        if "totalBytesBilled" in query
        else None,
    }


# Put BigQuery parameters in a consistent order for checking resumed jobs.
def canonical_parameters(parameters):
    config = bigquery.QueryJobConfig.from_api_repr({"query": {"queryParameters": parameters}})
    return sorted((p.to_api_repr() for p in config.query_parameters), key=lambda p: p["name"])


# Stop the run when a required condition fails and report the supplied reason.
def require(condition, message):
    if not condition:
        raise ValueError(message)


# Fingerprint the runner, settings, and stage files used to produce the results.
def code_hash():
    files = [ROOT / "run.py", ROOT / "runtime_defaults.json", ROOT / "stages_build.json"]
    files += sorted((ROOT / "sql").rglob("*.*")) + sorted((ROOT / "data").glob("*.json"))
    return digest(
        {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    )


# Fill in the configured project and baseline dataset in the default settings.
def runtime_defaults():
    # These bindings identify the operator's BigQuery installation.
    default_text = (ROOT / "runtime_defaults.json").read_text()
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    baseline = os.environ["GDELT_BASELINE_DATASET"]
    require(re.fullmatch(r"[a-z][a-z0-9-]+", project), "Invalid Google Cloud project.")
    require(
        re.fullmatch(r"[a-z][a-z0-9-]*\.[A-Za-z0-9_]+", baseline),
        "Use project.dataset for the GDELT baseline.",
    )
    require(RESERVATION, "Set GDELT_RESERVATION to the BigQuery reservation for this run.")
    return json.loads(
        default_text.replace("{project}", project).replace("{baseline_dataset}", baseline)
    )


# Create the initial run checkpoint for the chosen operating mode.
def prepare(path, mode):
    require(not path.exists(), "State already exists; use its original command to resume.")
    defaults = runtime_defaults()
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + secrets.token_hex(3)
    dataset = defaults["project"] + ".gdelt_minimal_" + suffix
    binding = defaults["mode_bindings"][mode]
    config = {
        "mode": mode,
        "dataset": dataset,
        "run_id": "gdelt_minimal_" + suffix,
        "project": defaults["project"],
        "location": defaults["location"],
        "created_at": now(),
        "code_sha256": code_hash(),
        "identifiers": {
            k: v.replace("{dataset}", dataset) for k, v in binding["identifiers"].items()
        },
        "parameters": defaults["parameters"] | binding["parameters"],
    }
    config["identifiers"]["baseline_dataset"] = defaults["baseline_dataset"]
    config["identifiers"]["dataset"] = dataset
    config["parameters"]["run_id"] = config["run_id"]
    save(path, {"config": config, "config_sha256": digest(config), "jobs": {}, "plans": {}})
    print(encode({"state": "PREPARED", "mode": mode, "dataset": dataset, "state_file": str(path)}))


class Run:
    # Load the checkpoint and bind it to the configuration and BigQuery client.
    def __init__(self, path, client=None):
        self.path, self.state = path, read(path)
        self.config = self.state["config"]
        require(digest(self.config) == self.state["config_sha256"], "Frozen configuration changed.")
        require(
            self.config["code_sha256"] == code_hash(),
            "Code changed; retain this state and its original code.",
        )
        require(
            re.fullmatch(r"[a-z][a-z0-9-]*\.gdelt_minimal_[a-z0-9_]+", self.config["dataset"]),
            "Destination must be an isolated gdelt_minimal dataset.",
        )
        self.stages, self.workflows = {}, {}
        for manifest in ["stages_build.json", "sql/evaluate/manifest.json"]:
            data = read(ROOT / manifest)
            self.stages.update(data["stages"])
            self.workflows.update(data["workflows"])
        self.client = client or bigquery.Client(
            project=self.config["project"], location=self.config["location"]
        )
        self.parameters = self.config["parameters"] | {
            "contract_json": encode(self.config),
            "contract_sha256": self.state["config_sha256"],
            "source_plan_json": encode(read(ROOT / "data/source_plan.json")),
        }

    # Persist this runner's latest checkpoint.
    def save(self):
        save(self.path, self.state)

    # Build a stage's SQL and query parameters from its settings and identifiers.
    def render(self, stage, extra=None, identifiers=None):
        spec = self.stages[stage]
        ids = self.config["identifiers"] | (identifiers or {})
        for value in ids.values():
            require(re.fullmatch(r"[A-Za-z0-9_.-]+", value), "Invalid SQL identifier.")
        sql = re.sub(r"\$\{(\w+)\}", lambda m: ids[m[1]], (ROOT / spec["sql"]).read_text())
        values = self.parameters | (extra or {})
        parameters = []
        for name, kind in spec["parameters"].items():
            value = values[name]
            parameters.append(
                bigquery.ArrayQueryParameter(name, kind[6:-1], value)
                if kind.startswith("ARRAY<")
                else bigquery.ScalarQueryParameter(name, kind, value)
            )
        return sql, parameters

    # Total billed bytes or outstanding reservations against the selected budget.
    def spent(self, kind):
        return sum(
            j.get("billed", j["reserved"]) for j in self.state["jobs"].values() if j["kind"] == kind
        )

    # Submit or resume one BigQuery stage and record its result and resource use.
    def step(self, stage, scope="", extra=None, identifiers=None, ceiling=None, inputs=0):
        spec = self.stages[stage]
        sql, parameters = self.render(stage, extra, identifiers)
        cap = PER_QUERY  # Legacy manifest/ceiling fields are not enforced as small limits.
        key = scope + "/" + stage
        intent = {
            "sql": sql,
            "parameters": canonical_parameters([p.to_api_repr() for p in parameters]),
            "cap": cap,
            "job_timeout_ms": JOB_TIMEOUT_MS,
        }
        signature = digest(intent)
        jobs = self.state["jobs"]
        if key not in jobs:
            kind = "derived" if spec["kind"] == "embedding" else spec["kind"]
            reserved = cap * spec["statement_count"]
            require(
                sum(j.get("inputs", 0) for j in jobs.values()) + inputs <= 2_000_000,
                "Logical embedding-input budget exhausted; nothing submitted.",
            )
            jobs[key] = {
                "job_id": "gdm_" + digest([self.config["run_id"], key, signature])[:48],
                "signature": signature,
                "kind": kind,
                "reserved": reserved,
                "cap": cap,
                "inputs": inputs,
                "state": "INTENT",
                "created_at": now(),
            }
            self.save()  # Persist intent BEFORE submitting any job, including a paid embedding.
        record = jobs[key]
        require(record["signature"] == signature, f"Changed operation: {key}; refusing replay.")
        require(
            record["state"] != "FAILED",
            f"Failed job retained: {record['job_id']}; inspect it before proceeding.",
        )
        if record["state"] == "DONE":
            return record.get("rows", [])
        config = bigquery.QueryJobConfig(
            query_parameters=parameters,
            maximum_bytes_billed=cap,
            use_query_cache=False,  # Use execution statistics from a fresh query.
            reservation=RESERVATION,
            job_timeout_ms=JOB_TIMEOUT_MS,
            labels={"gdelt_run": self.config["run_id"]},
        )
        try:
            job = self.client.get_job(record["job_id"], location=self.config["location"])
        except NotFound:
            require(
                record["state"] == "INTENT",
                "Previously observed job is missing; refusing a new submission.",
            )
            require(
                datetime.now(timezone.utc) - datetime.fromisoformat(record["created_at"])
                < timedelta(days=7),
                "Uncertain intent is too old for automatic resubmission; inspect the retained job identity.",
            )
            try:
                job = self.client.query(
                    sql, job_id=record["job_id"], job_config=config, job_retry=None
                )
            except Conflict:
                job = self.client.get_job(record["job_id"], location=self.config["location"])
        actual = job.to_api_repr()
        query = actual["configuration"]["query"]
        require(
            query["query"] == sql
            and int(query["maximumBytesBilled"]) == cap
            and canonical_parameters(query.get("queryParameters", [])) == intent["parameters"]
            and int(actual["configuration"].get("jobTimeoutMs", -1)) == JOB_TIMEOUT_MS
            and actual["configuration"].get("reservation") == RESERVATION
            and actual["configuration"].get("labels", {}).get("gdelt_run") == self.config["run_id"],
            "Recovered job differs from its frozen intent.",
        )
        record["state"] = "RUNNING"
        self.save()
        deadline = time.monotonic() + JOB_TIMEOUT_MS / 1000 + 60
        while job.state != "DONE":
            print(encode({"state": "WAITING", "stage": key, "job_id": job.job_id}), flush=True)
            if time.monotonic() >= deadline:
                job.cancel(timeout=60)
                raise RuntimeError(
                    f"Runaway timeout: cancellation requested for {job.job_id}; inspect before resuming."
                )
            time.sleep(10)
            job.reload()
        # Pinned SDK: QueryJob.to_api_repr() omits response statistics.
        stats = job._properties.get("statistics", {})
        record["statistics"] = {
            k: v for k, v in stats.items() if k in ("creationTime", "startTime", "endTime")
        }
        record["statistics"]["query"] = {
            k: v
            for k, v in stats.get("query", {}).items()
            if k
            in (
                "totalBytesBilled",
                "totalBytesProcessed",
                "totalSlotMs",
                "totalServicesSkuSlotMs",
                "statementType",
                "vectorSearchStatistics",
            )
        }
        if not job.error_result and "totalBytesBilled" in stats.get("query", {}):
            record["billed"] = int(stats["query"]["totalBytesBilled"])
        if job.error_result:
            record.update(state="FAILED", error=job.error_result)
            self.save()
            raise RuntimeError(
                f"Job failed; retained without replay: {job.job_id}: {job.error_result}"
            )
        if stats.get("query", {}).get("statementType") == "SCRIPT":
            children = list(self.client.list_jobs(parent_job=job.job_id, max_results=129))
            require(
                len(children) <= 128,
                "Unexpectedly many script statements; inspect the retained parent job.",
            )
            record["children"] = []
            for child in children:
                child = self.client.get_job(child.job_id, location=self.config["location"])
                child_api = child._properties
                record["children"].append(
                    measured_job(key, child.job_id, child_api.get("statistics", {}))
                    | {
                        "parent_job_id": job.job_id,
                        "destination": child_api.get("configuration", {})
                        .get("query", {})
                        .get("destinationTable"),
                    }
                )
        rows = []
        if spec["max_result_rows"]:
            limit = spec["max_result_rows"]
            result = job.result(
                max_results=limit + 1, page_size=min(100, limit + 1), job_retry=None, timeout=60
            )
            aliases = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
            fields = {f.name: aliases.get(f.field_type, f.field_type) for f in result.schema}
            require(
                fields == spec["fields"] and all(f.mode != "REPEATED" for f in result.schema),
                f"Unexpected control-result schema: {key}",
            )
            rows = [dict(row) for row in result]
            require(
                len(rows) <= limit and len(encode(rows).encode()) <= 1_000_000,
                "Control result exceeded its bound.",
            )
        record.update(state="DONE", rows=rows, completed_at=now())
        self.save()
        print(encode({"state": "DONE", "stage": key, "job_id": job.job_id}), flush=True)
        return rows

    # Execute the registered stages for a named workflow in order.
    def flow(self, workflow, scope="", extra=None, identifiers=None):
        for stage in self.workflows[workflow]:
            self.step(stage, scope, extra, identifiers)

    # Validate or establish the BigQuery dataset that will receive this run's results.
    def destination(self):
        name = self.config["dataset"]
        try:
            dataset = self.client.get_dataset(name)
        except NotFound:
            dataset = bigquery.Dataset(name)
            dataset.location = self.config["location"]
            dataset.labels = {"gdelt_run": self.config["run_id"]}
            dataset = self.client.create_dataset(dataset)
        require(
            dataset.location == self.config["location"]
            and (dataset.labels or {}).get("gdelt_run") == self.config["run_id"],
            "Destination exists but does not belong to this run.",
        )

    # Verify that external tables and settings match the inputs bound to this run.
    def bindings(self):
        defaults = runtime_defaults()
        names = set(defaults["mode_bindings"]["saved"]["identifiers"].values())
        names = {n for n in names if n.count(".") == 2}
        names.add(defaults["baseline_dataset"] + ".market_closes")
        if self.config["mode"] == "filtered":
            names.update(
                defaults["baseline_dataset"] + "." + n
                for n in ["eval500_normalized_v01", "eval500_parsed_v01"]
            )
        snapshots = {}
        for name in sorted(names):
            table = self.client.get_table(name)
            require(table.table_type == "TABLE", f"Expected a physical input table: {name}")
            snapshots[name] = {
                "rows": table.num_rows,
                "modified": table.to_api_repr()["lastModifiedTime"],
                "schema": [f.to_api_repr() for f in table.schema],
            }
        for name, count in [
            ("corpus_documents", 2_000_000),
            ("advance_dense_documents_v01", 2_000_000),
            ("market_closes", 2515),
        ]:
            require(
                snapshots[defaults["baseline_dataset"] + "." + name]["rows"] == count,
                f"Baseline count changed: {name}",
            )
        if "bindings" in self.state:
            require(
                snapshots == self.state["bindings"],
                "Baseline input tables changed since this run was bound.",
            )
        else:
            self.state["bindings"] = snapshots
            self.save()
        if self.config["mode"] == "filtered":
            binding = read(ROOT / "data/source_binding.json")
            table = self.client.get_table(binding["source_table"])
            columns = {f.name: (f.field_type, f.mode) for f in table.schema}
            require(
                all(
                    columns.get(v["source_name"]) == (v["source_type"], v["source_mode"])
                    for v in binding["columns"].values()
                ),
                "Public GKG source projection changed.",
            )
            require(
                table.time_partitioning is not None
                and table.time_partitioning.field is None
                and table.time_partitioning.type_ == "DAY",
                "Expected daily ingestion-time partitions.",
            )

    # Choose historical source batches and retain the plan for reproducible retries.
    def source_plan(self, cap, deficient=None):
        key = str(cap)
        if key in self.state["plans"]:
            return self.state["plans"][key]
        tasks = []
        for month in read(ROOT / "data/source_months.json"):
            days = [d for d in month["days"] if deficient is None or d in deficient]
            if not days:
                continue
            end = (datetime.fromisoformat(max(days)) + timedelta(days=1)).date().isoformat()
            params = {
                "batch_month": month["month"],
                "candidate_limit": cap,
                "eligible_days": days,
                "partition_start": min(days) + "T00:00:00+00:00",
                "partition_end": end + "T00:00:00+00:00",
                "source_collection": 1,
            }
            sql, parameters = self.render("source_month_select", params)
            config = bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False, query_parameters=parameters
            )
            job = self.client.query(sql, job_config=config, job_retry=None)
            estimate = job.total_bytes_processed
            require(estimate is not None, "Source dry run did not report bytes.")
            require(
                estimate <= PER_QUERY,
                "Source estimate exceeds 1 TB for one month; inspect date/partition bounds.",
            )
            tasks.append({"parameters": params, "ceiling": PER_QUERY, "estimated_bytes": estimate})
        self.state["plans"][key] = tasks
        self.save()
        print(
            encode(
                {
                    "state": "PREFLIGHT",
                    "candidate_limit": cap,
                    "months": len(tasks),
                    "source_estimated_bytes": sum(t["estimated_bytes"] for t in tasks),
                    "per_query_runaway_bytes": PER_QUERY,
                }
            ),
            flush=True,
        )
        return tasks

    # Construct the historical corpus, lexical tables, and vector index.
    def build(self):
        require(
            self.config["mode"] == "filtered",
            "Prepare a separate --mode filtered state for a build.",
        )
        self.bindings()
        self.source_plan(6000)
        self.destination()
        if not self.state.get("corpus_frozen"):
            self.flow("corpus_setup")
            deficient = None
            for cap in [6000, 12000, 24000]:
                for task in self.source_plan(cap, deficient):
                    self.step(
                        "source_month",
                        f"source/{cap}/{task['parameters']['batch_month']}",
                        task["parameters"],
                        ceiling=task["ceiling"],
                    )
                self.flow("corpus_derive", str(cap))
                deficient = {str(r["observed_day"]) for r in self.step("deficient_days", str(cap))}
                if not deficient:
                    break
            require(not deficient, "Frozen daily quotas remain deficient; no embeddings submitted.")
            self.flow("corpus_freeze")
            self.step("corpus_status")
            self.step("corpus_manifest")
            self.state["corpus_frozen"] = True
            self.save()
        self.flow("embedding_prepare")
        preparation = self.step("dense_preparation_status")[0]
        batches = self.step("embedding_batches")
        require(
            sum(b["expected_inputs"] for b in batches) == preparation["new_inputs"]
            and all(0 < b["expected_inputs"] <= 25000 for b in batches),
            "Embedding plan differs from its receipt.",
        )
        for batch in batches:
            params = {"batch_number": batch["batch_number"]}
            scope = f"batch/{batch['batch_number']}"
            self.step("dense_batch_before", scope, params)
            self.step("dense_embed", scope, params, inputs=batch["expected_inputs"])
            self.step("dense_batch_after", scope, params)
        self.flow("embedding_publish")
        self.step("dense_final_status")
        self.flow("lexical")
        self.step("bm25_status")
        self.flow("index")
        poll = self.state.get("index_poll", 0)
        rows = self.step("index_status", f"poll/{poll}")
        ready = (
            len(rows) == 1
            and rows[0]["index_status"] == "ACTIVE"
            and rows[0]["coverage_percentage"] == 100
        )
        ready = (
            ready
            and rows[0]["unindexed_row_count"] == 0
            and rows[0]["last_refresh_time"] is not None
        )
        if not ready or rows[0]["disable_reason"] is not None:
            self.state["index_poll"] = poll + 1
            self.save()
            print(
                encode(
                    {"state": "INDEX_BUILDING", "next": "Repeat build with the same state file."}
                )
            )
            return
        self.flow("verification_prepare")
        self.step("verify_preparation_status")
        self.state["built"] = True
        self.save()
        self.evaluate()

    # Run the retrieval comparisons and build the evidence reports.
    def evaluate(self):
        require(
            self.config["mode"] == "saved" or self.state.get("built"),
            "Finish the filtered build first.",
        )
        if self.state.get("complete"):
            print(
                encode(
                    {
                        "state": "COMPLETE",
                        "report_receipt": self.state["report_receipt"],
                        "reused": True,
                    }
                )
            )
            return
        self.bindings()
        self.destination()
        for stage in self.workflows["evaluate"]:
            extra = None
            if stage == "evaluate_reports":
                if "report_inputs" not in self.state:
                    self.state["report_inputs"] = {
                        "performance_json": encode(self.performance()),
                        "report_context_json": encode(
                            self.config
                            | {
                                "market_as_of_utc": "Frozen completed two-million-corpus market contract",
                                "source_as_of_utc": self.parameters["as_of_utc"],
                                "window_start": self.parameters["window_start"],
                            }
                        ),
                    }
                    self.save()
                extra = self.state["report_inputs"]
            self.step(stage, extra=extra)
            if stage == "evaluate_ivf40":
                self.index_evidence()
        table = self.client.get_table(self.config["dataset"] + ".full2m_eval_report_pages_v01")
        receipt = {
            "project": self.config["project"],
            "dataset": self.config["dataset"].split(".")[1],
            "table": table.table_id,
            "run_id": self.config["run_id"],
            "last_modified_time": table.to_api_repr()["lastModifiedTime"],
            "expected_rows": table.num_rows,
        }
        receipt_path = self.path.with_name(self.path.stem + "_reports.json")
        save(receipt_path, receipt)
        self.state["complete"] = True
        self.state["report_receipt"] = str(receipt_path)
        self.save()
        print(
            encode(
                {
                    "state": "COMPLETE",
                    "dataset": self.config["dataset"],
                    "report_receipt": str(receipt_path),
                    "accuracy": self.state["jobs"]["/evaluate_accuracy"]["rows"][0],
                }
            )
        )

    # Check the saved job evidence for actual use of the vector index.
    def index_evidence(self):
        record = self.state["jobs"]["/evaluate_ivf40"]
        if not any(
            e.get("indexUsageMode") == "FULLY_USED" for e in record.get("index_evidence", [])
        ):
            evidence = [record["statistics"].get("query", {}).get("vectorSearchStatistics", {})]
            children = self.client.list_jobs(parent_job=record["job_id"], max_results=16)
            for child in children:
                job = self.client.get_job(child.job_id, location=self.config["location"])
                evidence.append(
                    job._properties.get("statistics", {})
                    .get("query", {})
                    .get("vectorSearchStatistics", {})
                )
            record["index_evidence"] = evidence
            self.save()
        require(
            any(e.get("indexUsageMode") == "FULLY_USED" for e in record["index_evidence"]),
            "ANN job has not demonstrated FULLY_USED vector-index evidence; inspect its retained job.",
        )

    # Summarize the measured resource use and timing of the retrieval jobs.
    def performance(self):
        ledger = self.state["jobs"]
        jobs = [measured_job(k, v["job_id"], v.get("statistics", {})) for k, v in ledger.items()]
        retrieval = [
            j for j in jobs if j["key"] in ["/evaluate_ivf40", "/evaluate_bm25", "/evaluate_fusion"]
        ]
        seconds = (
            sum(j["execution_seconds"] for j in retrieval)
            if len(retrieval) == 3 and all(j["execution_seconds"] is not None for j in retrieval)
            else None
        )
        count = ledger["/evaluate_matches_status"]["rows"][0]["completed_query_documents"]
        return {
            "run_id": self.config["run_id"],
            "mode": self.config["mode"],
            "jobs": jobs,
            "scope": "Retrieval seconds sum parent server execution times for cosine, BM25, and fusion; "
            "queueing, validation, exact-reference, build and report work are excluded. "
            "Other listed jobs describe this run only. This snapshot precedes its own materialization.",
            "processing_seconds": seconds,
            "documents_per_processing_second": count / seconds if seconds else None,
            "completed_query_documents": count,
            "stages": [
                {"phase": j["key"], "elapsed_seconds": j["execution_seconds"]} for j in retrieval
            ],
            "accuracy": ledger["/evaluate_accuracy"]["rows"][0],
            "index_use_mode": "FULLY_USED",
            "child_statement_timings": [
                child for j in ledger.values() for child in j.get("children", [])
            ],
            "source_bytes_observed_or_reserved": self.spent("source"),
            "derived_bytes_observed_or_reserved": self.spent("derived"),
            "embedding_inputs": sum(j.get("inputs", 0) for j in ledger.values()),
            "production_capacity_proven": False,
        }

    # Prepare and filter a time-bounded batch of incoming GDELT documents.
    def intake(self, start, raw_limit):
        require(
            self.state.get("built"), "Current-news admission requires a completed filtered build."
        )
        begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
        require(begin.tzinfo is not None, "Supply an explicit UTC offset for --window-start.")
        begin = begin.astimezone(timezone.utc)
        end = begin + timedelta(hours=1)
        require(end <= datetime.now(timezone.utc), "The supplied hour must already be closed.")
        batch = "b_" + digest([begin.isoformat(), raw_limit])[:12]
        windows = self.state.setdefault("intake_windows", {})
        if batch not in windows:
            windows[batch] = {
                "window_start": begin.isoformat(),
                "window_end": end.isoformat(),
                "as_of_utc": now(),
                "partition_start": begin.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).isoformat(),
                "partition_end": (
                    (end - timedelta(microseconds=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    + timedelta(days=1)
                ).isoformat(),
                "raw_limit": raw_limit,
            }
            self.save()
        self.flow("live_prepare", batch, windows[batch], {"batch_id": batch})
        print(
            encode(
                {
                    "state": "INTAKE_PREPARED",
                    "batch_id": batch,
                    "boundary": "Validated request candidates; live embeddings and scheduling belong to the caller.",
                }
            )
        )


# Select the index build, evaluation, intake, or reporting operation from the command line.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["prepare", "status", "preflight", "evaluate", "build", "intake"]
    )
    parser.add_argument("--state", type=Path, default=Path("gdelt_state.json"))
    parser.add_argument(
        "--mode", choices=["saved", "filtered"], default="saved", help="Used only by prepare."
    )
    parser.add_argument(
        "--window-start", help="One closed hour, for intake only, e.g. 2026-11-06T18:00:00Z."
    )
    parser.add_argument("--raw-limit", type=int, choices=[6000, 12000, 24000], default=6000)
    args = parser.parse_args()
    args.state = args.state.resolve()
    with args.state.with_suffix(args.state.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            prepare(args.state, args.mode)
        elif args.command == "status":
            state = read(args.state)
            print(
                encode(
                    {
                        "config": state["config"],
                        "complete": state.get("complete", False),
                        "jobs": {
                            k: {f: v.get(f) for f in ["job_id", "state", "billed", "inputs"]}
                            for k, v in state["jobs"].items()
                        },
                    }
                )
            )
        else:
            run = Run(args.state)
            if args.command == "preflight":
                run.bindings()
                if run.config["mode"] == "filtered":
                    run.source_plan(6000)
                print(encode({"state": "PREFLIGHT_COMPLETE", "dataset_created": False}))
            elif args.command == "intake":
                require(args.window_start, "intake requires --window-start.")
                run.intake(args.window_start, args.raw_limit)
            else:
                getattr(run, args.command)()


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(
            encode(
                {
                    "state": "STOPPED",
                    "error": str(error),
                    "resume": "Keep the state file and original code.",
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)
