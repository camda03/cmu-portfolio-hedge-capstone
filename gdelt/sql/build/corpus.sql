CREATE OR REPLACE TABLE `${dataset}.corpus_documents` PARTITION BY observed_day CLUSTER BY
  corpus_rank,
  document_id AS
SELECT
  m.document_id AS document_id,
  m.corpus_rank AS corpus_rank,
  m.observed_day AS observed_day,
  m.in_checkpoint_100k AS in_checkpoint_100k,
  m.in_checkpoint_1m AS in_checkpoint_1m,
  n.observed_at AS observed_at,
  n.source_record_key AS source_record_key,
  n.source_revision_key AS source_revision_key,
  n.raw_revision_sha256 AS raw_revision_sha256,
  n.document_url AS document_url,
  n.title AS title,
  n.metadata_text AS metadata_text,
  n.metadata_sha256 AS metadata_sha256,
  n.lex_tokens AS lex_tokens,
  n.quality_flags AS quality_flags,
  p.anchor_date AS market_anchor_date,
  p.day20_date AS market_day20_date
FROM `${dataset}.corpus_membership` AS m
JOIN `${dataset}.normalized_observations` AS n ON n.raw_revision_sha256=m.raw_revision_sha256
AND n.document_id=m.document_id
JOIN `${dataset}.market_paths` AS p ON p.observed_day=m.observed_day
AND p.eligible
WHERE n.admissible;
