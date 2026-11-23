SELECT
  batch_number,
  expected_inputs
FROM `${dataset}.filtered_dense_batches_v01`
ORDER BY batch_number;
