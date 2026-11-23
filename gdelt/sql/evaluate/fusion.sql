-- Reciprocal rank fusion gives the two searches equal weight. A missing match contributes zero.
-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TABLE `${dataset}.full2m_eval_matches_v01`
CLUSTER BY query_document_id AS
WITH combined AS (
  SELECT
    COALESCE(c.query_document_id, b.query_document_id) AS query_document_id,
    COALESCE(c.historical_document_id, b.historical_document_id) AS historical_document_id,
    c.cosine_distance AS cosine_distance,
    c.cosine_similarity AS cosine_similarity,
    c.cosine_rank AS cosine_rank,
    b.bm25_score_exact AS bm25_score_exact,
    b.bm25_score AS bm25_score,
    b.bm25_rank AS bm25_rank,
    IF(c.cosine_rank IS NULL, 0.0, 1.0 / (60.0 + c.cosine_rank))  -- A missing rank contributes zero.
      + IF(b.bm25_rank IS NULL, 0.0, 1.0 / (60.0 + b.bm25_rank)) AS rrf_score
  FROM `${dataset}.full2m_eval_ivf40_matches_v01` AS c
  FULL OUTER JOIN `${dataset}.full2m_eval_bm25_matches_v01` AS b
    ON b.query_document_id = c.query_document_id
    AND b.historical_document_id = c.historical_document_id
)
SELECT
  c.query_document_id AS query_document_id,
  c.historical_document_id AS historical_document_id,
  c.cosine_distance AS cosine_distance,
  c.cosine_similarity AS cosine_similarity,
  c.cosine_rank AS cosine_rank,
  c.bm25_score_exact AS bm25_score_exact,
  c.bm25_score AS bm25_score,
  c.bm25_rank AS bm25_rank,
  c.rrf_score AS rrf_score,
  ROW_NUMBER() OVER (
    PARTITION BY c.query_document_id
    ORDER BY c.rrf_score DESC, c.historical_document_id
  ) AS fused_rank
FROM combined AS c;
