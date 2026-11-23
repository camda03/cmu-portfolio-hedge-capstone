CREATE TABLE `${dataset}.filtered_dense_vectors_v01` CLUSTER BY
  request_id,
  full_input_rank AS
SELECT
  p.request_id AS request_id,
  p.full_input_rank AS full_input_rank,
  p.first_corpus_rank AS first_corpus_rank,
  p.batch_number AS batch_number,
  p.cache_source_kind AS cache_source_kind,
  k.embedding AS embedding
FROM `${dataset}.filtered_dense_inputs_v01` AS p
JOIN `${dataset}.filtered_dense_cache_v01` AS k ON k.request_id=p.request_id
WHERE p.batch_number=0
UNION ALL BY NAME
SELECT
  p.request_id AS request_id,
  p.full_input_rank AS full_input_rank,
  p.first_corpus_rank AS first_corpus_rank,
  p.batch_number AS batch_number,
  p.cache_source_kind AS cache_source_kind,
  e.outcome.result AS embedding
FROM `${dataset}.filtered_dense_inputs_v01` AS p
JOIN `${dataset}.filtered_dense_raw_v01` AS e ON e.request_id=p.request_id
AND e.batch_number=p.batch_number
WHERE p.batch_number BETWEEN 1 AND 80
  AND e.batch_number BETWEEN 1 AND 80;
