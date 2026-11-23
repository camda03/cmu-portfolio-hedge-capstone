SELECT
  i.table_name AS table_name,
  i.index_name AS index_name,
  i.index_status AS index_status,
  i.coverage_percentage AS coverage_percentage,
  i.unindexed_row_count AS unindexed_row_count,
  i.last_refresh_time AS last_refresh_time,
  i.disable_reason AS disable_reason
FROM `${dataset}.INFORMATION_SCHEMA.VECTOR_INDEXES` AS i
WHERE i.table_name='filtered_dense_vectors_v01'
  AND i.index_name='filtered_dense_vector_index_v01'
LIMIT 2
