CREATE TABLE `${dataset}.filtered_dense_documents_v01` PARTITION BY observed_day CLUSTER BY
  request_id,
  document_id AS
SELECT
  h.document_id AS document_id,
  h.metadata_sha256 AS metadata_sha256,
  p.request_id AS request_id,
  h.corpus_rank AS corpus_rank,
  h.observed_day AS observed_day
FROM `${dataset}.corpus_documents` AS h
JOIN `${dataset}.filtered_dense_inputs_v01` AS p ON p.metadata_sha256=h.metadata_sha256
WHERE p.batch_number BETWEEN 0 AND 80;
