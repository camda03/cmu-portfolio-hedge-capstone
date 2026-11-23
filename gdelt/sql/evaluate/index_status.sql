-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_index_status AS
SELECT
  table_name,
  index_name,
  index_status,
  coverage_percentage,
  unindexed_row_count,
  last_refresh_time,
  disable_reason
FROM `${dense_dataset}.INFORMATION_SCHEMA.VECTOR_INDEXES`
WHERE table_name=@dense_table_name
  AND index_name=@dense_index_name
LIMIT 2;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF(index_status='ACTIVE'
               AND coverage_percentage=100
               AND unindexed_row_count=0
               AND last_refresh_time IS NOT NULL
               AND disable_reason IS NULL)=1
   FROM evaluation_index_status) AS 'The selected vector index must be fully ready';


SELECT
  table_name,
  index_name,
  index_status,
  coverage_percentage,
  unindexed_row_count,
  last_refresh_time,
  disable_reason
FROM evaluation_index_status
LIMIT 1;
