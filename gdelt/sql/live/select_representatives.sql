ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT raw_revision_sha256)
   FROM `${dataset}.filtered_live_${batch_id}_normalized_v01`) AS 'Normalized current-source revisions are not one to one';

ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT raw_revision_sha256)
   FROM `${dataset}.filtered_live_${batch_id}_parsed_v01`) AS 'Parsed current-source revisions are not one to one';

ASSERT
  (SELECT COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_normalized_v01`)=
  (SELECT COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_parsed_v01`) AS 'Parsed and normalized current-source populations differ';

ASSERT
  (SELECT COUNT(*)=0
   FROM `${dataset}.filtered_live_${batch_id}_normalized_v01` AS n
   LEFT JOIN `${dataset}.filtered_live_${batch_id}_parsed_v01` AS p ON p.raw_revision_sha256=n.raw_revision_sha256
   WHERE p.raw_revision_sha256 IS NULL
     OR (n.admissible
         AND ARRAY_LENGTH(p.p.parse_reasons) IS DISTINCT
         FROM 0)) AS 'Current-source normalization has a missing or invalid parsed revision';


CREATE TABLE `${dataset}.filtered_live_${batch_id}_representatives_v01` CLUSTER BY document_id AS
SELECT
  n.*,
  COUNT(*) OVER(PARTITION BY n.document_id) AS sample_admissible_revision_count
FROM `${dataset}.filtered_live_${batch_id}_normalized_v01` AS n
WHERE n.admissible
  AND ARRAY_LENGTH(n.lex_tokens)>0 QUALIFY ROW_NUMBER() OVER(PARTITION BY n.document_id
                                                             ORDER BY n.observed_at, n.source_revision_key, n.raw_revision_sha256)=1;
