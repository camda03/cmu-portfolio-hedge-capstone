# First baseline for unattended monitoring

Keep every existing analyst role and its search responsibilities. The additional
SOL discovery packet is evidence for all seven stages. The final Revision must
address every discovery event and retain material uncertainty; discovery does not
establish exhaustive coverage. Distinguish when an event occurred, when it became
public, and when this research found it. Do not assign invented publication times.

In the normal single forecast JSON object, add `monitoring_handoff`. This is a
first baseline, so there are no incoming reassessment referrals to invent. Use
the exact new forecast ID supplied in the research context. Supply only:

- `forecast_id`: that new ID.
- `baseline_summary`: substantive accepted premises, mitigation and limits.
- `conditions`: objects containing `id`, `aliases` (list of strings), `baseline`,
  `reassess_when`, `log_watch_when`, `timing`, `source_refs` (list of strings).
- `research_questions`: objects containing `id`, `owner`, `question`, `status`,
  `source_refs` (list of strings).
- `warning_continuity`: objects containing `id`, `disposition` (`retained`,
  `revised`, `resolved`, or `superseded`), `destination_ids` (nonempty list of
  condition/research IDs), and an explanatory `reason`.
- `coverage_gaps`: list of remaining limitations.
- `price_policy`: explain that the research workflow has no qualified mechanical price trigger
  or order routing; dated broker observations retain their actual dates and scope.

Include all warning families from the original assigned analysis, not just the
discovery findings. Include favorable resolution and worsening developments.
Condition and research IDs must be unique; prefix them with the supplied symbol.
All text fields and supporting source references must be nonempty. Current analyst
cards may be cited as DOC-CURRENT-BUSINESS, DOC-CURRENT-VALUATION,
DOC-CURRENT-CATALYSTS, DOC-CURRENT-GEOECONOMICS, DOC-CURRENT-DRAFT,
DOC-CURRENT-REVIEW, or DOC-CURRENT-REPORT; those cards must themselves attribute
consequential claims. Monitoring performs finite triage, not repeated full analysis
of unchanged facts. Include at least one condition and warning record. The final
report must also retain the shared discovery review and discovery_dispositions.
