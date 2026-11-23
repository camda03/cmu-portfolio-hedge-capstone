CREATE OR REPLACE TABLE `${dataset}.unfiltered_candidate_representatives` PARTITION BY observed_day CLUSTER BY document_id AS WITH chosen AS
  (SELECT
     n.document_id AS document_id,
     ARRAY_AGG(STRUCT(n.observed_at AS observed_at, n.observed_day AS observed_day, n.gkg_record_id AS gkg_record_id, n.source_record_key AS source_record_key, n.source_revision_key AS source_revision_key, n.raw_revision_sha256 AS raw_revision_sha256)
               ORDER BY n.observed_at, n.gkg_record_id, n.source_revision_key, n.raw_revision_sha256
               LIMIT 1)[OFFSET(0)] AS representative
   FROM `${dataset}.normalized_observations` AS n
   JOIN `${dataset}.market_paths` AS m ON m.observed_day=n.observed_day
   AND m.eligible
   WHERE n.admissible
     AND ARRAY_LENGTH(n.lex_tokens)>0
   GROUP BY n.document_id)
SELECT
  c.document_id AS document_id,
  c.representative.observed_at AS observed_at,
  c.representative.observed_day AS observed_day,
  c.representative.source_record_key AS source_record_key,
  c.representative.source_revision_key AS source_revision_key,
  c.representative.raw_revision_sha256 AS raw_revision_sha256,
  LOWER(TO_HEX(SHA256(TO_JSON_STRING([@sample_seed,'document-order',c.document_id])))) AS document_order
FROM chosen AS c;
