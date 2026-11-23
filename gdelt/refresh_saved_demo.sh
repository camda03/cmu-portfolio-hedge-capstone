#!/usr/bin/env bash
# Run on the existing worker. Reuse its completed index and installed research code.
set +x
set -euo pipefail
umask 077
gdelt_mode="${1:-start}"
gdelt_runs="${GDELT_RUNS_DIR:?Supply an absolute output directory for refreshes}"
gdelt_build="${GDELT_BUILD_DIR:?Supply the directory with the completed index state}"
gdelt_research="${GDELT_RESEARCH_DIR:?Supply the directory containing research_run.sh}"
gdelt_python="${GDELT_PYTHON:?Supply the Python interpreter for the build}"

case "$gdelt_mode" in
  status)
    gdelt_run=$(cat "$gdelt_runs/latest.txt")
    date -u
    printf 'REFRESH_DIRECTORY: %s\n' "$gdelt_run"
    tail -n 45 "$gdelt_run/refresh.log"
    exit ;;
  start|resume)
    test -s "$gdelt_build/quota_recovery_state.json"
    test -x "$gdelt_python"
    test -f "$gdelt_research/research_run.sh"
    mkdir -p "$gdelt_runs"
    exec 8>"$gdelt_runs/.refresh.lock"
    if ! flock -n 8; then
      printf 'REFRESH_ALREADY_RUNNING: use status; no second run started.\n'
      exit 0
    fi
    if [[ "$gdelt_mode" == start ]]; then
      gdelt_run=$(mktemp -d "$gdelt_runs/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
      cp -- "${BASH_SOURCE[0]}" "$gdelt_run/refresh.sh"
      printf '%s\n' "$gdelt_run" > "$gdelt_runs/latest.txt.tmp"
      mv -f -- "$gdelt_runs/latest.txt.tmp" "$gdelt_runs/latest.txt"
    else
      gdelt_run=$(cat "$gdelt_runs/latest.txt")
      if [[ -f "$gdelt_run/COMPLETE" ]]; then
        printf 'REFRESH_ALREADY_COMPLETE: %s\n' "$gdelt_run"
        exit 0
      fi
    fi
    # The child inherits lock 8; disconnecting the operator does not stop the run.
    nohup bash "$gdelt_run/refresh.sh" _work "$gdelt_run" >> "$gdelt_run/refresh.log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$gdelt_run/pid.txt"
    printf 'REFRESH_STARTED: pid=%s directory=%s\n' "$!" "$gdelt_run"
    printf 'Completion requires GDELT_REFRESH_READY in refresh.log.\n'
    exit ;;
  _work)
    gdelt_run="${2:?Missing saved refresh directory}"
    [[ "$gdelt_run" == "$gdelt_runs/"* && -d "$gdelt_run" ]]
    trap 'gdelt_exit=$?; if (( gdelt_exit != 0 )); then printf "GDELT_REFRESH_STOPPED: exit=%s; retain this directory and use resume.\n" "$gdelt_exit"; fi' EXIT ;;
  *) printf 'Use start, status, or resume.\n' >&2; exit 2 ;;
esac

date -u
printf 'Refreshing one closed news hour with the existing one-hour ingestion buffer.\n'
cd "$gdelt_build"
(
  exec 7>.demo-launch.lock
  flock -n 7 || { printf 'The original GDELT launcher is active.\n' >&2; exit 1; }
  "$gdelt_python" -u - run --build-state quota_recovery_state.json \
    --state "$gdelt_run/demo_state.json" <<'PY'
import run
import demo
from recovery.resume import RecoveryRun

run.Run = RecoveryRun
demo.main()
PY
)

GDELT_RESEARCH_LOG="$gdelt_run/refresh.log" bash "$gdelt_research/research_run.sh" \
  "$gdelt_run/demo_state_reports.json" "$gdelt_run/research_output"

# Reuse the existing receipt/hash validator; print the actual cohort and window.
cd "$gdelt_research"
python3 - "$gdelt_run/research_output" <<'PY'
import json
from pathlib import Path
import sys
from research_evidence import load_evidence

folder = Path(sys.argv[1])
packet = load_evidence(folder)
handoff = json.loads((folder / "handoff.json").read_bytes())
assert handoff["schema"] == "gdelt-research-handoff-v2" and handoff["status"] == "completed"
assert handoff["run_id"] == packet["receipt"]["run_id"]
assert handoff["input_sha256"] == packet["input_sha256"]
assert handoff["evidence_files"] == packet["receipt"]["files"]
assert handoff["evidence_summary"] == packet["summary"]
assert handoff["disclosure"] == json.loads((folder / "disclosure.json").read_bytes())
print(json.dumps({"state": "REFRESH_VERIFIED", "run_id": handoff["run_id"],
                  "query_documents": packet["summary"]["query_documents"],
                  "observation_window": handoff["disclosure"]["observation_window"]}))
PY
tar -czf "$gdelt_run/gdelt-packet.tgz.tmp" -C "$gdelt_run/research_output" \
  handoff.json receipt.json summary.json articles.json paths.json disclosure.json
mv -- "$gdelt_run/gdelt-packet.tgz.tmp" "$gdelt_run/gdelt-packet.tgz"
sha256sum "$gdelt_run/gdelt-packet.tgz"
printf '%s\n' "$gdelt_run" > "$gdelt_runs/latest_complete.txt.tmp"
mv -f -- "$gdelt_runs/latest_complete.txt.tmp" "$gdelt_runs/latest_complete.txt"
touch "$gdelt_run/COMPLETE"
printf 'GDELT_REFRESH_READY: %s/research_output/index.html\nPACKET: %s/gdelt-packet.tgz\n' "$gdelt_run" "$gdelt_run"
