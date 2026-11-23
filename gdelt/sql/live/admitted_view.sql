CREATE VIEW `${dataset}.filtered_live_${batch_id}_admitted_documents_v01` AS
SELECT
  r.*,
  f.full_theme_count,
  f.matched_rule_ids,
  f.rules_sha256
FROM `${dataset}.filtered_live_${batch_id}_representatives_v01` AS r
JOIN `${dataset}.filtered_live_${batch_id}_filter_decisions_v01` AS f ON f.document_id=r.document_id
AND f.raw_revision_sha256=r.raw_revision_sha256
WHERE f.expanded_rule_selected
  AND f.rules_sha256='ed6f55b8ee8450f5faedb009d7f4ff1854d4707c5f0c8ab59755712d5b254b77';
