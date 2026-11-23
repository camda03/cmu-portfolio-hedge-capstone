CREATE TABLE `${dataset}.filtered_live_${batch_id}_filter_decisions_v01` CLUSTER BY document_id AS WITH
  labels AS
  (SELECT
     r.document_id,
     r.raw_revision_sha256,
     r.observed_day,
     ARRAY
     (SELECT DISTINCT normalized_label
      FROM
        (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label, NFKC), r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ')) AS normalized_label
         FROM UNNEST(p.p.themes) AS label)
      WHERE normalized_label!=''
      ORDER BY normalized_label) AS themes,
     r.sample_admissible_revision_count
   FROM `${dataset}.filtered_live_${batch_id}_representatives_v01` AS r
   JOIN `${dataset}.filtered_live_${batch_id}_parsed_v01` AS p ON p.raw_revision_sha256=r.raw_revision_sha256),
  evaluated AS
  (SELECT
     document_id,
     raw_revision_sha256,
     observed_day,
     sample_admissible_revision_count,
     ARRAY_LENGTH(themes) AS full_theme_count,
     `${dataset}.gkg_expanded_rule_ids_v01`(themes) AS matched_rule_ids
   FROM labels)
SELECT
  *,
  ARRAY_LENGTH(matched_rule_ids)>0 AS expanded_rule_selected,
  'ed6f55b8ee8450f5faedb009d7f4ff1854d4707c5f0c8ab59755712d5b254b77' AS rules_sha256,
  CASE
      WHEN ARRAY_LENGTH(matched_rule_ids)>0 THEN 'ADMITTED'
      WHEN full_theme_count=0 THEN 'EXCLUDED_NO_THEMES'
      ELSE 'EXCLUDED_NO_MATCH'
  END AS admission_status
FROM evaluated;

ASSERT
  (SELECT COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_filter_decisions_v01`) =
  (SELECT COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_representatives_v01`) AS 'Every selected canonical revision must have one filter decision';
