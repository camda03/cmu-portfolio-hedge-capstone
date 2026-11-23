#!/usr/bin/env bash
# Run in the isolated research checkout; completed BigQuery reports are only read.
set +x
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

research_receipt="${1:?Supply the completed GDELT report receipt}"
research_output="${2:-$PWD/research_output}"
research_model="${GDELT_RESEARCH_MODEL:-gpt-6.2-astra}"
research_reasoning="${GDELT_RESEARCH_REASONING:-max}"
exec 9>.research-run.lock
flock -n 9 || { printf 'A GDELT research run is already active.\n' >&2; exit 1; }
printf '%s\n' "$$" > research_pid.txt
if [[ -n "${GDELT_RESEARCH_LOG:-}" ]]; then
  printf '%s\n' "$GDELT_RESEARCH_LOG" > research_latest_log.txt
fi
test -f "$research_receipt" || { printf 'Completed report receipt not found: %s\n' "$research_receipt" >&2; exit 1; }

if [[ ! -x .venv-research/bin/python ]]; then
  python3 -m venv .venv-research
fi
.venv-research/bin/python -m pip install --quiet -r requirements.txt -r requirements-research.txt -r requirements-display.txt
.venv-research/bin/python -u research_evidence.py export \
  --receipt "$research_receipt" --output "$research_output"
.venv-research/bin/python -u research_languages.py --output "$research_output"

: "${OPENAI_API_KEY:?Supply the model credential for this research job}"
# Repeating this command resumes saved provider response IDs or reuses completion.
if .venv-research/bin/python -u research.py --evidence "$research_output" \
    --model "$research_model" --reasoning "$research_reasoning"; then
  .venv-research/bin/python research_evidence.py render --output "$research_output"
  printf 'GDELT_RESEARCH_READY: %s/index.html and %s/handoff.json\n' "$research_output" "$research_output"
else
  research_exit=$?
  .venv-research/bin/python research_evidence.py render --output "$research_output"
  printf 'GDELT_RESEARCH_STOPPED: charts remain available; retain the output and resume this same command.\n' >&2
  exit "$research_exit"
fi
