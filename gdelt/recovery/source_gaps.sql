-- Verify the suspected gaps directly, before changing the sampling allocation.
-- Only the eighteen reviewed public-table partitions are queried.

CREATE TABLE `${dataset}.recovery_source_gaps_v01` AS WITH source_counts AS
  (SELECT
     DATE(_PARTITIONTIME) AS observed_day,
     COUNT(*) AS source_rows
   FROM `gdelt-bq.gdeltv2.gkg_partitioned`
   WHERE _PARTITIONTIME IN UNNEST(@missing_partitions)
   GROUP BY observed_day)
SELECT
  q.observed_day,
  q.target_documents AS original_target,
  COALESCE(s.source_rows, 0) AS source_rows,
  CURRENT_TIMESTAMP() AS checked_at
FROM `${dataset}.daily_quotas` AS q
LEFT JOIN source_counts AS s USING(observed_day)
WHERE TIMESTAMP(q.observed_day) IN UNNEST(@missing_partitions);

ASSERT
  (SELECT COUNT(*)=18
   AND COUNT(DISTINCT observed_day)=18
   AND SUM(original_target)=10034
   AND COUNTIF(source_rows!=0)=0
   FROM `${dataset}.recovery_source_gaps_v01`) AS 'Source-gap check disagrees: inspect recovery_source_gaps_v01; quotas were NOT changed';
