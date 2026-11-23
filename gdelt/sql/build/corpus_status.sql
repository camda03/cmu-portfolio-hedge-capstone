SELECT
  COUNT(*) AS documents,
  COUNT(DISTINCT c.document_id) AS distinct_documents,
  COUNT(DISTINCT c.corpus_rank) AS distinct_ranks,
  MIN(c.corpus_rank) AS first_rank,
  MAX(c.corpus_rank) AS last_rank,
  COUNTIF(c.in_checkpoint_100k) AS checkpoint_100k_documents,
  COUNTIF(c.in_checkpoint_1m) AS checkpoint_1m_documents,
  COUNTIF(ARRAY_LENGTH(c.lex_tokens)=0
          OR c.metadata_text IS NULL
          OR BYTE_LENGTH(c.metadata_text)>6000) AS invalid_documents,
  COUNT(DISTINCT c.metadata_sha256) AS distinct_metadata_inputs,
  COUNT(DISTINCT c.observed_day) AS represented_days
FROM `${dataset}.corpus_documents` AS c
LIMIT 1
