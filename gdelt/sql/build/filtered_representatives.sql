CREATE OR REPLACE TABLE `${dataset}.candidate_representatives` PARTITION BY observed_day CLUSTER BY document_id AS
SELECT r.*
FROM `${dataset}.unfiltered_candidate_representatives` AS r
JOIN `${dataset}.candidate_filter_decisions` AS f ON f.document_id=r.document_id
AND f.raw_revision_sha256=r.raw_revision_sha256
WHERE f.expanded_rule_selected;
