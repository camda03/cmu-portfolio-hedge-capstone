CREATE TABLE `${dataset}.filtered_dense_requests_v01` CLUSTER BY request_id AS
SELECT
  LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', h.metadata_sha256)))) AS request_id,
  h.metadata_sha256 AS metadata_sha256,
  h.metadata_text AS model_input,
  COUNT(*) AS document_uses,
  MIN(h.corpus_rank) AS first_corpus_rank
FROM `${dataset}.corpus_documents` AS h
GROUP BY
  h.metadata_sha256,
  h.metadata_text;
