ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT k.request_id)
   AND COUNT(*)<=2000000
   FROM `${dataset}.filtered_dense_cache_v01` AS k) AS 'Copied cache request identities must be unique before allocation';
