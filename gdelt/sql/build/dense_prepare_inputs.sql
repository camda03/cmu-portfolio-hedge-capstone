CREATE TEMP TABLE allocation_keys AS
SELECT
  p.request_id AS request_id,
  p.first_corpus_rank AS first_corpus_rank,
  IF(k.request_id IS NULL, 0, 1) AS cache_source_kind,
  DIV(p.first_corpus_rank-1, 25000) AS rank_bucket
FROM `${dataset}.filtered_dense_requests_v01` AS p
LEFT JOIN `${dataset}.filtered_dense_cache_v01` AS k ON k.request_id=p.request_id;


CREATE TEMP TABLE allocation_ranks AS WITH
  bucket_counts AS
  (SELECT
     k.rank_bucket AS rank_bucket,
     COUNT(*) AS input_count,
     COUNTIF(k.cache_source_kind=0) AS new_input_count
   FROM allocation_keys AS k
   GROUP BY k.rank_bucket),
  bucket_offsets AS
  (SELECT
     b.rank_bucket AS rank_bucket,
     COALESCE(SUM(b.input_count) OVER(
                                      ORDER BY b.rank_bucket ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS full_rank_offset,
     COALESCE(SUM(b.new_input_count) OVER(
                                          ORDER BY b.rank_bucket ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS new_rank_offset
   FROM bucket_counts AS b),
  local_ranks AS
  (SELECT
     k.request_id AS request_id,
     k.first_corpus_rank AS first_corpus_rank,
     k.cache_source_kind AS cache_source_kind,
     k.rank_bucket AS rank_bucket,
     ROW_NUMBER() OVER(PARTITION BY k.rank_bucket
                       ORDER BY k.first_corpus_rank, k.request_id) AS local_full_rank,
     IF(k.cache_source_kind=0, COUNTIF(k.cache_source_kind=0) OVER(PARTITION BY k.rank_bucket
                                                                   ORDER BY k.first_corpus_rank, k.request_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), NULL) AS local_new_rank
   FROM allocation_keys AS k),
  rank_map AS
  (SELECT
     k.request_id AS request_id,
     k.first_corpus_rank AS first_corpus_rank,
     k.cache_source_kind AS cache_source_kind,
     o.full_rank_offset+k.local_full_rank AS full_input_rank,
     IF(k.cache_source_kind=0, o.new_rank_offset+k.local_new_rank, NULL) AS new_input_rank,
     IF(k.cache_source_kind=0, 1+DIV(o.new_rank_offset+k.local_new_rank-1, 25000), 0) AS batch_number
   FROM local_ranks AS k
   JOIN bucket_offsets AS o ON o.rank_bucket=k.rank_bucket)
SELECT
  r.request_id AS request_id,
  r.first_corpus_rank AS first_corpus_rank,
  r.cache_source_kind AS cache_source_kind,
  r.full_input_rank AS full_input_rank,
  r.new_input_rank AS new_input_rank,
  r.batch_number AS batch_number
FROM rank_map AS r;


CREATE TABLE `${dataset}.filtered_dense_inputs_v01` PARTITION BY RANGE_BUCKET(batch_number, GENERATE_ARRAY(0, 81, 1)) CLUSTER BY request_id OPTIONS(require_partition_filter=true) AS
SELECT
  p.request_id AS request_id,
  p.metadata_sha256 AS metadata_sha256,
  p.model_input AS model_input,
  p.document_uses AS document_uses,
  p.first_corpus_rank AS first_corpus_rank,
  r.full_input_rank AS full_input_rank,
  r.cache_source_kind AS cache_source_kind,
  r.new_input_rank AS new_input_rank,
  r.batch_number AS batch_number
FROM `${dataset}.filtered_dense_requests_v01` AS p
JOIN allocation_ranks AS r ON r.request_id=p.request_id
AND r.first_corpus_rank=p.first_corpus_rank;
