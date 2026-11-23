CREATE TABLE `${dataset}.filtered_dense_batches_v01` CLUSTER BY batch_number AS
SELECT
  p.batch_number AS batch_number,
  COUNT(*) AS expected_inputs,
  COUNT(*) AS input_cap,
  MIN(p.new_input_rank) AS first_new_rank,
  MAX(p.new_input_rank) AS last_new_rank,
  LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(p.request_id, '\t', p.metadata_sha256, '\n'), ''
                                 ORDER BY p.new_input_rank)))) AS batch_sha256,
  CURRENT_TIMESTAMP() AS recorded_at_utc
FROM `${dataset}.filtered_dense_inputs_v01` AS p
WHERE p.batch_number BETWEEN 1 AND 80
GROUP BY p.batch_number;
