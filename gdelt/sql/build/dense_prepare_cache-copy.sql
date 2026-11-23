CREATE TABLE `${dataset}.filtered_dense_cache_v01` CLUSTER BY request_id AS WITH old_bindings AS
  (SELECT
     m.request_id AS request_id,
     m.metadata_sha256 AS metadata_sha256,
     h.metadata_text AS model_input
   FROM `${baseline_dataset}.advance_dense_documents_v01` AS m
   JOIN `${baseline_dataset}.corpus_documents` AS h ON h.document_id=m.document_id
   AND h.metadata_sha256=m.metadata_sha256
   JOIN `${dataset}.filtered_dense_requests_v01` AS p ON p.request_id=m.request_id
   AND p.metadata_sha256=m.metadata_sha256
   AND p.model_input=h.metadata_text
   GROUP BY
     m.request_id,
     m.metadata_sha256,
     h.metadata_text)
SELECT
  p.request_id AS request_id,
  p.metadata_sha256 AS metadata_sha256,
  p.model_input AS model_input,
  1 AS source_kind,
  '${baseline_dataset}' AS source_dataset,
  v.embedding AS embedding,
  LOWER(TO_HEX(SHA256(TO_JSON_STRING(v.embedding)))) AS vector_sha256
FROM `${dataset}.filtered_dense_requests_v01` AS p
JOIN old_bindings AS h ON h.request_id=p.request_id
AND h.metadata_sha256=p.metadata_sha256
AND h.model_input=p.model_input
JOIN `${baseline_dataset}.advance_dense_vectors_v01` AS v ON v.request_id=p.request_id;
