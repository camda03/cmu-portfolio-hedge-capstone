SELECT

  (SELECT COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_raw_v01`) AS sampled_raw_revisions,

  (SELECT COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_normalized_v01`) AS normalized_revisions,

  (SELECT COUNTIF(admissible)
   FROM `${dataset}.filtered_live_${batch_id}_normalized_v01`) AS admissible_revisions,

  (SELECT COUNTIF(NOT admissible)
   FROM `${dataset}.filtered_live_${batch_id}_normalized_v01`) AS normalization_rejected_revisions,

  (SELECT COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_representatives_v01`) AS selected_canonical_documents,

  (SELECT COUNTIF(expanded_rule_selected)
   FROM `${dataset}.filtered_live_${batch_id}_filter_decisions_v01`) AS expanded_documents,

  (SELECT COUNTIF(admission_status='EXCLUDED_NO_MATCH')
   FROM `${dataset}.filtered_live_${batch_id}_filter_decisions_v01`) AS excluded_no_match_documents,

  (SELECT COUNTIF(admission_status='EXCLUDED_NO_THEMES')
   FROM `${dataset}.filtered_live_${batch_id}_filter_decisions_v01`) AS excluded_no_theme_documents,
  COUNTIF(h.document_id IS NOT NULL) AS expanded_history_overlaps,
  COUNTIF(h.document_id IS NULL) AS expanded_fresh_documents,
  GREATEST(500-COUNTIF(h.document_id IS NULL), 0) AS missing_for_500
FROM `${dataset}.filtered_live_${batch_id}_admitted_documents_v01` AS a
LEFT JOIN `${dataset}.corpus_documents` AS h ON h.document_id=a.document_id;
