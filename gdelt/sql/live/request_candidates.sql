CREATE TABLE `${dataset}.filtered_live_${batch_id}_request_candidates_v01` CLUSTER BY request_id AS
SELECT
  q.query_request_id AS request_id,
  q.metadata_sha256,
  MIN(q.metadata_text) AS model_input,
  COUNT(*) AS document_uses,
  COUNT(DISTINCT q.metadata_text) AS input_variant_count,
  'ed6f55b8ee8450f5faedb009d7f4ff1854d4707c5f0c8ab59755712d5b254b77' AS rules_sha256
FROM `${dataset}.filtered_live_${batch_id}_query_documents_v01` AS q
GROUP BY
  q.query_request_id,
  q.metadata_sha256;

ASSERT
  (SELECT COUNT(*) BETWEEN 1 AND 500
   AND COUNT(DISTINCT request_id)=COUNT(*)
   AND SUM(document_uses)=500
   AND COUNTIF(input_variant_count!=1
               OR r.model_input IS NULL
               OR LENGTH(TRIM(r.model_input))=0
               OR BYTE_LENGTH(r.model_input)>6000
               OR r.metadata_sha256 IS DISTINCT
               FROM LOWER(TO_HEX(SHA256(r.model_input)))
               OR r.request_id IS DISTINCT
               FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', r.metadata_sha256)))))=0
   FROM `${dataset}.filtered_live_${batch_id}_request_candidates_v01` AS r) AS 'Filtered request candidates must bind exactly to the selected 500 documents';
