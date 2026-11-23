CREATE TABLE `${dataset}.filtered_dense_raw_v01` (
                                                    batch_number INT64,
                                                    request_id STRING,outcome STRUCT<result ARRAY<FLOAT64>,status STRING>,recorded_at_utc TIMESTAMP) PARTITION BY RANGE_BUCKET(
                                                                                                                                                                                 batch_number,
                                                                                                                                                                                 GENERATE_ARRAY(0, 81, 1)) CLUSTER BY request_id OPTIONS(require_partition_filter=true);
