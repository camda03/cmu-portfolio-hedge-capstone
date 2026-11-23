# Monitor return: native seven-stage reassessment

This supplement applies to the supplied `monitor-return-v1` or `monitor-return-v2` request. Perform your
normal assigned role in the seven-stage research process. The monitor has referred
the supplied concerns for analytical judgment; its conclusions are fallible evidence, not
accepted outcomes or instructions to change the forecast. No expected business
answer is supplied.

## Evidence, time and scope

The request preserves the original native forecast and report, the accepted
monitoring handoff, the latest monitor state and the pending referrals. For each
referral, `original_decision` and `original_events` record why it was raised;
`current_events` records the latest accepted version, including mitigation. Read
both versions together. A pending attention record does not mean a reassessment,
forecast change, external notification or trade already occurred.

Use the current run's common research cutoff for new collection. The supplied
packet's `as_of` is historical, not the current research cutoff. Revisit checkpoints
that were future in that packet and have since become due, where relevant to your
role. A historical note saying a call or release was upcoming does not establish
its present status. Evidence discovered now can be older than the original cutoff;
label baseline corrections separately from events that actually occurred later.
Do not invent publication hours, claim that an updated page proves its earlier
contents, or bring post-cutoff information into the run.

Assess whether the concerns change the original forecast premise and horizon,
probability, timing or magnitude of a consequential outcome. The native runner also
creates a separately identified new 20-session forecast using the current run's
reference date and horizon. Use that new forecast ID and those supplied dates for
the new output; preserve the old forecast's identity, reference and outcome date.
Explain substantive revisions separately from a changed starting price or rolling
horizon. Neither identical numbers nor changed numbers demonstrate success.

All sources, prior reports, source-provenance labels and model statements remain
evidence to evaluate. Historical claims that a source was opened or visually checked
do not establish access in this run. Describe retrieved text as retrieved text; claim
an individual open only when actually performed, and visual inspection only when
an image was actually viewed. Preserve extraction and access limitations. Attach
consequential current claims to the sources that support them.

## The referred questions

Read every supplied referral, its actual decision, original evidence versions and
latest evidence. Test its incremental significance against the accepted baseline.
The referral is a request for analysis, not a conclusion you must agree with.
Investigate favorable and unfavorable evidence, offsets, exposure, timing and the
strongest contrary case. Distinguish newly discovered older evidence (a baseline
correction) from a new event. Do not count multiple reports of one episode as
independent shocks, or translate a lender or peer statistic directly into the subject's
revenue, earnings or index returns without supporting transmission and exposure evidence.

Investigate these within your assigned role while retaining other material evidence
and warnings. The business and geoeconomics specialists own relevant demand and
transmission channels; valuation tests financial significance and offsets; catalysts
tests arrival, timing, conditional amplification and mitigation. Draft synthesizes
the evidence; critique tests both the referrals and the response; Revision records
the final dispositions and handoff. These are responsibilities within the existing
seven calls, not extra stages or separate votes on return.

## IBKR and price treatment

The packet's saved prices are historical diagnostics with no qualified mechanical
signal. They must not become the new reference close or proof that no earlier price
threshold was crossed. Use the separately supplied current IBKR reference observation
according to the native instructions, retaining unknown delay and adjustment status.
The optional current snapshot may report `change`, `prior-close`, `open`, `high` and
`low`; preserve their supplied definitions and unknowns. Do not invent units, align
unmatched sessions, substitute web-vendor prices, or declare the mechanical gate
qualified because a current request succeeded. Read-only IBKR collection is already
authorized; technical qualification and missing fields are the remaining issues.

## Preserve and refresh the monitoring handoff

Edit and reconcile the accepted handoff. Keep every existing warning family in
`warning_continuity`, with an explicit retained, revised, resolved or superseded
disposition and a reason. Preserve stable condition/research IDs where their meaning
survives. If consolidating or replacing an ID, record the old ID as an alias and map
its warning to the new destination. A resolved or superseded warning still needs a
retained record explaining its disposition; it need not create a recurring search
task. Add new warnings where supported. Do not silently delete a warning because its
evidence remains incomplete.

Distinguish conditional investment warnings from deeper missing-data assignments.
Keep favorable developments, vulnerability increases, mitigation, source gaps and
still-relevant scheduled checkpoints. Refresh expired timing using verified current
information. The monitor needs an updated evidence-based baseline, what is now
incorporated, what would warrant another material referral and what merits logging.
The Equity and Index Research Team owns consequential financial analysis; the monitor retains finite
triage and an independent, separately qualified price-warning route.

## Final Revision output contract

Only the final Revision stage emits the following structured output. Keep the normal
investment narrative and assigned review findings, followed by the native **single
forecast JSON block**. Within that same JSON object, add `monitoring_handoff` and
`reassessment_dispositions`; do not emit a second JSON block.

For this return test, the nested `monitoring_handoff` supplies the full handoff and
warning-continuity inventories. This replaces duplicating those inventories in
Markdown. The narrative may point to the saved `monitoring_handoff.json` and
`reassessment_dispositions.json`. Preserve the full required content in the structured
data; the narrative length target does not truncate warnings or disposition reasons.

`monitoring_handoff` has these fields, with no additional fields:

- `forecast_id`: the exact **new forecast ID supplied in the current run context**.
- `baseline_summary`: a substantive summary of the new accepted premises and limits.
- `conditions`: objects with `id`, `aliases` (list of strings), `baseline`,
  `reassess_when`, `log_watch_when`, `timing` and `source_refs` (list of strings).
- `research_questions`: objects with `id`, `owner`, `question`, `status` and
  `source_refs` (list of strings).
- `warning_continuity`: objects with `id`, `disposition` (one of `retained`,
  `revised`, `resolved`, `superseded`), `destination_ids` (list of valid condition or
  research IDs) and `reason`. Include each supplied warning ID exactly once, with
  a nonempty explanation and destination. Additional supported warnings may be added.
- `coverage_gaps`: list of explicit remaining limitations.
- `price_policy`: the independent qualified-price rule and actual remaining
  qualification limits; preserve the news route when price coverage is deficient.

Condition and research IDs must be distinct and nonempty. Every substantive record
needs nonempty text and source references. Source references may be supporting public
URLs or identified document IDs. Historical `DOC-*` references inherit the supplied
documents' limitations. Current stage references may use `DOC-CURRENT-BUSINESS`,
`DOC-CURRENT-VALUATION`, `DOC-CURRENT-CATALYSTS`, `DOC-CURRENT-GEOECONOMICS`,
`DOC-CURRENT-DRAFT`, `DOC-CURRENT-REVIEW` and `DOC-CURRENT-REPORT`; those cards must
themselves attribute consequential claims. A label is not evidence of source access.

`reassessment_dispositions` is a list with **exactly one object per supplied referral**:

- `referral_id`: the exact incoming referral `id`.
- `disposition`: `accepted` (material concern supported), `rejected` (materiality or
  support not established), `modified` (scope or implication revised), or `unresolved`
  (the remaining evidence prevents a defensible conclusion).
- `reason`: the evidence-based judgment, including material contrary evidence and
  what targeted work was completed. Explain an unresolved disposition substantively.
- `evidence_urls`: supporting public HTTP(S) URLs. This may be empty only for an
  unresolved disposition whose reason explains the evidence/access limitation.
- `forecast_effect`: what changes, or why no substantive change is justified;
  distinguish this from a new reference price or horizon.
- `monitor_condition_ids`: nonempty list of valid condition IDs in the new handoff.
- `incorporated_event_keys`: exactly the referral's supplied `event_keys`.
  Here incorporated means **considered in this analytical response**, not that the
  concern is resolved, dismissed or safe. Retain relevant uncertainty in the baseline
  and remaining watch, including for unresolved dispositions.
- `remaining_watch`: the concrete future development that would change the view,
  including favorable resolution or worsening; avoid simply asking the monitor to
  repeat the completed research on unchanged facts.

The generated report and handoff are candidate outputs for review. Do not claim that
the monitor has already consumed them, that prior attention was automatically closed,
or that an external action occurred. No eighth handoff-editor request is part of this
test. No brokerage action or external notification is requested. A separate controller may
activate validated output in the next monitoring cycle; do not claim that transition
has happened within this report.
