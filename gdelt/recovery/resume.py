"""One reviewed sampling amendment; reuse r2 data and its unchanged BigQuery runner."""

import copy
from datetime import date, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import run as engine

DATASET = os.environ.get("GDELT_RECOVERY_DATASET", "")
DAYS = ["2017-08-29"] + [(date(2025, 6, 15) + timedelta(days=i)).isoformat() for i in range(17)]
POLICY = {
    "version": "same-month-gap-reallocation-v1",
    "original_dataset": DATASET,
    "target_documents": 2000000,
    "reassigned_slots": 10034,
    "planned_days": 3626,
    "represented_days": 3608,
    "source_gap_dates": DAYS,
    "method": "Retain available original daily allocations; distribute missing slots round-robin within the same calendar month using the original seeded day order, subject to actual capacity.",
    "outcomes_used_in_reallocation": False,
    "limitations": "These dates cannot appear as historical hits. Missing coverage is not absence of real-world events. Monthly totals are retained, but daily sampling weights change. Original corpus and prior research are not rewritten.",
    "audit_table": DATASET + ".recovery_quota_manifest_v01",
}
NOTICE = (
    '<section><p class="note">Historical coverage limitation: 18 source dates are missing '
    "(2017-08-29 and 2025-06-15 through 2025-07-01). Their 10,034 sample slots were "
    "redistributed within the same calendar months. Two million qualifying documents "
    "remain the target; missing coverage does not mean no events occurred.</p></section>"
)


def fingerprint():
    return engine.digest(
        {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(HERE.iterdir())
            if p.suffix in (".py", ".sql")
        }
    )


class RecoveryRun(engine.Run):
    def __init__(self, path, client=None):
        super().__init__(path, client=client)
        self.stages["recovery_gaps"] = {
            "sql": "recovery/source_gaps.sql",
            "parameters": {"missing_partitions": "ARRAY<TIMESTAMP>"},
            "maximum_bytes_billed": engine.PER_QUERY,
            "statement_count": 2,
            "kind": "source",
            "max_result_rows": 0,
            "fields": {},
        }
        self.stages["recovery_quotas"] = {
            "sql": "recovery/quotas.sql",
            "parameters": {"policy_json": "STRING"},
            "maximum_bytes_billed": engine.PER_QUERY,
            "statement_count": 20,
            "kind": "derived",
            "max_result_rows": 0,
            "fields": {},
        }
        self.stages["corpus_guard"]["sql"] = "recovery/corpus_guard.sql"
        self.stages["corpus_guard"]["statement_count"] = 6

    def render(self, stage, extra=None, identifiers=None):
        if stage == "evaluate_reports":
            extra = dict(extra or {})
            context = json.loads(extra["report_context_json"])
            context["sampling_amendment"] = POLICY
            extra["report_context_json"] = engine.encode(context)
        sql, parameters = super().render(stage, extra, identifiers)
        if stage == "evaluate_reports":
            engine.require(
                sql.count("</nav></header><main>") == 3,
                "Unexpected report template; stop before publishing.",
            )
            sql = sql.replace("</nav></header><main>", "</nav></header><main>" + NOTICE)
        return sql, parameters


def reviewed_state(original):
    engine.require(
        original.config["dataset"] == DATASET and original.config["mode"] == "filtered",
        "This recovery is only for the reviewed r2 dataset.",
    )
    state = original.state
    engine.require(
        not state.get("corpus_frozen") and not state.get("built") and not state.get("complete"),
        "Original run advanced beyond the reviewed stopping point.",
    )
    engine.require(
        all(j["state"] == "DONE" for j in state["jobs"].values())
        and sum(j.get("inputs", 0) for j in state["jobs"].values()) == 0,
        "Unfinished jobs or historical embeddings exist; do not duplicate work.",
    )
    rows = state["jobs"]["24000/deficient_days"]["rows"]
    engine.require(
        len(rows) == 18
        and {str(r["observed_day"]) for r in rows} == set(DAYS)
        and all(r["available"] == 0 for r in rows)
        and sum(r["missing_documents"] for r in rows) == 10034,
        "The shortfall differs from the reviewed 18-date/10,034-slot case.",
    )
    engine.require("6000" in state["plans"], "The original completed source plan is required.")
    return state


def main():
    original_path = ROOT / "filtered_state.json"
    path = ROOT / "quota_recovery_state.json"
    # Hold the original launcher/state locks as well: neither build may run concurrently.
    with (
        (ROOT / ".demo-launch.lock").open("a") as launch,
        original_path.with_suffix(".json.lock").open("a") as oldlock,
        path.with_suffix(".json.lock").open("a") as lock,
    ):
        for handle in (launch, oldlock, lock):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = engine.Run(original_path)
        original_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
        if not path.exists():
            state = copy.deepcopy(reviewed_state(original))
            state.update(
                recovery_code_sha256=fingerprint(),
                original_state_sha256=original_hash,
                sampling_amendment=POLICY,
            )
            engine.save(path, state)
        runner = RecoveryRun(path)
        engine.require(
            runner.state.get("recovery_code_sha256") == fingerprint()
            and runner.state.get("original_state_sha256") == original_hash,
            "Recovery code or original state changed; preserve both and inspect.",
        )
        runner.bindings()
        runner.destination()
        if not runner.state.get("corpus_frozen"):
            runner.step(
                "recovery_gaps", extra={"missing_partitions": [d + "T00:00:00Z" for d in DAYS]}
            )
            print(
                "SOURCE_GAPS_VERIFIED: 18 specified source partitions contain no rows.", flush=True
            )
            runner.step("recovery_quotas", extra={"policy_json": engine.encode(POLICY)})
            print(
                "QUOTAS_REALLOCATED: 10034 slots; same months; original allocation archived.",
                flush=True,
            )
            runner.flow("corpus_freeze")
            runner.step("corpus_status")
            runner.step("corpus_manifest")
            runner.state["corpus_frozen"] = True
            runner.save()
            print(
                "CORPUS_FROZEN: exactly 2000000 qualifying documents across 3608 available dates.",
                flush=True,
            )
        for _ in range(120):
            runner.build()  # Saved source plan is reused; corpus_frozen skips extraction and filtering.
            if runner.state.get("complete"):
                break
            time.sleep(60)
        else:
            raise RuntimeError(
                "Index still pending; resume this same recovery with its saved state."
            )
    # demo.py acquires its own locks. Propagate the sampling amendment into fresh reports too.
    import demo
    from types import SimpleNamespace

    engine.Run = RecoveryRun
    demo.run_batch(
        SimpleNamespace(
            build_state=path, state=ROOT / "demo_state.json", window_start=None, raw_limit=None
        )
    )
    output = ROOT / "demo_output"
    if not (output / "receipt.json").exists():
        demo.outputs(
            SimpleNamespace(
                command="export", receipt=ROOT / "demo_state_reports.json", output=output
            )
        )
    print(
        "DEMO_READY: demo_state_reports.json and demo_output; coverage amendment included.",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(
            json.dumps(
                {
                    "state": "STOPPED",
                    "error": str(error),
                    "resume": "Keep both states and run this same recovery; never restart the old launcher.",
                }
            ),
            flush=True,
        )
        raise SystemExit(1)
