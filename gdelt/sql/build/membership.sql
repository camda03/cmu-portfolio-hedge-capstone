CREATE OR REPLACE TABLE `${dataset}.corpus_membership` PARTITION BY observed_day CLUSTER BY
  corpus_rank,
  document_id AS WITH
  daily_order AS
  (SELECT
     r.document_id AS document_id,
     r.observed_day AS observed_day,
     r.raw_revision_sha256 AS raw_revision_sha256,
     q.target_documents AS target_documents,
     q.stratum_order AS stratum_order,
     ROW_NUMBER() OVER (PARTITION BY r.observed_day
                        ORDER BY r.document_order, r.document_id) AS day_rank
   FROM `${dataset}.candidate_representatives` AS r
   JOIN `${dataset}.daily_quotas` AS q ON q.observed_day=r.observed_day),
  selected AS
  (SELECT
     d.document_id AS document_id,
     d.observed_day AS observed_day,
     d.raw_revision_sha256 AS raw_revision_sha256,
     d.day_rank AS day_rank,
     d.stratum_order AS stratum_order,
     CAST(2*d.day_rank-1 AS BIGNUMERIC)/CAST(2*d.target_documents AS BIGNUMERIC) AS priority
   FROM daily_order AS d
   WHERE d.day_rank<=d.target_documents),
  bucketed AS
  (SELECT
     s.document_id AS document_id,
     s.observed_day AS observed_day,
     s.raw_revision_sha256 AS raw_revision_sha256,
     s.day_rank AS day_rank,
     s.stratum_order AS stratum_order,
     s.priority AS priority,
     CAST(FLOOR(s.priority*1024) AS INT64) AS rank_bucket
   FROM selected AS s),
  within_bucket AS
  (SELECT
     b.document_id AS document_id,
     b.observed_day AS observed_day,
     b.raw_revision_sha256 AS raw_revision_sha256,
     b.day_rank AS day_rank,
     b.rank_bucket AS rank_bucket,
     ROW_NUMBER() OVER (PARTITION BY b.rank_bucket
                        ORDER BY b.priority, b.stratum_order, b.observed_day, b.document_id) AS bucket_rank
   FROM bucketed AS b),
  sizes AS
  (SELECT
     b.rank_bucket AS rank_bucket,
     COUNT(*) AS bucket_size
   FROM bucketed AS b
   GROUP BY b.rank_bucket),
  offsets AS
  (SELECT
     s.rank_bucket AS rank_bucket,
     COALESCE(SUM(s.bucket_size) OVER (
                                       ORDER BY s.rank_bucket ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS rank_offset
   FROM sizes AS s)
SELECT
  b.document_id AS document_id,
  b.observed_day AS observed_day,
  b.raw_revision_sha256 AS raw_revision_sha256,
  b.day_rank AS day_rank,
  o.rank_offset+b.bucket_rank AS corpus_rank,
  o.rank_offset+b.bucket_rank<=100000 AS in_checkpoint_100k,
  o.rank_offset+b.bucket_rank<=1000000 AS in_checkpoint_1m
FROM within_bucket AS b
JOIN offsets AS o ON o.rank_bucket=b.rank_bucket;
