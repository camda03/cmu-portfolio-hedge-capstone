ASSERT
  (SELECT COUNT(*)=0
   FROM `${dataset}.filtered_dense_raw_v01` AS e
   LEFT JOIN `${dataset}.filtered_dense_inputs_v01` AS p ON p.request_id=e.request_id
   AND p.batch_number=e.batch_number
   AND p.cache_source_kind=0
   AND p.batch_number BETWEEN 1 AND 80
   WHERE e.batch_number BETWEEN 0 AND 80
     AND p.request_id IS NULL) AS 'Saved inference outputs must belong to their frozen batch';
