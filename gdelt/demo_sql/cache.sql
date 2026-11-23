-- Reuse only exact request identities from the verified filtered build.

CREATE TABLE `${dataset}.demo_cache_v01` CLUSTER BY request_id AS
SELECT
  v.request_id,
  v.embedding
FROM `${dense_vectors}` AS v
JOIN `${intake_requests}` AS r ON r.request_id=v.request_id;

ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT request_id)
   FROM `${dataset}.demo_cache_v01`) AS 'Cache identities must be unique';


CREATE TABLE `${query_requests}` CLUSTER BY request_id AS
SELECT
  r.*,
  c.request_id IS NOT NULL AS cached
FROM `${intake_requests}` AS r
LEFT JOIN `${dataset}.demo_cache_v01` AS c USING(request_id);

ASSERT
  (SELECT COUNT(*) BETWEEN 1 AND 500
   AND COUNT(*)=COUNT(DISTINCT request_id)
   AND SUM(document_uses)=@expected_query_documents
   AND COUNTIF(input_variant_count IS DISTINCT
               FROM 1
               OR metadata_sha256 IS DISTINCT
               FROM LOWER(TO_HEX(SHA256(model_input)))
               OR model_input IS NULL
               OR BYTE_LENGTH(model_input) NOT BETWEEN 1 AND 6000)=0
   FROM `${query_requests}`) AS 'Live requests must bind to the admitted query cohort';
