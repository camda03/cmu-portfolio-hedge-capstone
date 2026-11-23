-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.
-- Both branches sum full-corpus BIGNUMERIC term contributions without candidate pruning.
-- Filtered mode excludes identical inputs before top20; baseline reuses each lexical query ranking.
IF @exclude_identical_inputs THEN
CREATE TABLE `${dataset}.full2m_eval_bm25_matches_v01` CLUSTER BY query_document_id AS WITH
  batch_terms AS
  (SELECT DISTINCT q.term_key AS term_key
   FROM `${dataset}.full2m_eval_query_terms_v01` AS q),
  candidate_scores AS
  (SELECT
     q.query_key AS query_key,
     p.historical_key AS historical_key,
     SUM(p.term_score_exact) AS bm25_score_exact
   FROM `${bm25_weights}` AS p
   JOIN `${dataset}.full2m_eval_query_terms_v01` AS q ON q.term_key=p.term_key
   WHERE p.term_key IN
       (SELECT b.term_key
        FROM batch_terms AS b)
   GROUP BY
     q.query_key,
     p.historical_key),
  eligible AS
  (SELECT
     q.query_document_id AS query_document_id,
     q.query_request_id AS query_request_id,
     h.historical_document_id AS historical_document_id,
     m.request_id AS matched_request_id,
     s.bm25_score_exact AS bm25_score_exact
   FROM candidate_scores AS s
   JOIN `${dataset}.full2m_eval_queries_v01` AS q ON q.query_key=s.query_key
   JOIN `${bm25_history}` AS h ON h.historical_key=s.historical_key
   JOIN `${dense_documents}` AS m ON m.document_id=h.historical_document_id
   WHERE h.historical_document_id!=q.query_document_id
     AND m.request_id!=q.query_request_id),
  best AS
  (SELECT
     e.query_document_id AS query_document_id,
     ARRAY_AGG(STRUCT(e.query_request_id AS query_request_id, e.historical_document_id AS historical_document_id, e.matched_request_id AS matched_request_id, e.bm25_score_exact AS bm25_score_exact)
               ORDER BY e.bm25_score_exact DESC, e.historical_document_id
               LIMIT 20) AS matches
   FROM eligible AS e
   GROUP BY e.query_document_id)
SELECT
  b.query_document_id AS query_document_id,
  x.query_request_id AS query_request_id,
  x.historical_document_id AS historical_document_id,
  x.matched_request_id AS matched_request_id,
  x.bm25_score_exact AS bm25_score_exact,
  CAST(x.bm25_score_exact AS FLOAT64) AS bm25_score,
  rank_offset+1 AS bm25_rank
FROM best AS b
CROSS JOIN UNNEST(b.matches) AS x WITH
OFFSET AS rank_offset;

ELSE
CREATE TABLE `${dataset}.full2m_eval_bm25_matches_v01` CLUSTER BY query_document_id AS WITH
  batch_terms AS
  (SELECT DISTINCT q.term_key AS term_key
   FROM `${dataset}.full2m_eval_query_terms_v01` AS q),
  candidate_scores AS
  (SELECT
     q.query_key AS query_key,
     p.historical_key AS historical_key,
     SUM(p.term_score_exact) AS bm25_score_exact
   FROM `${bm25_weights}` AS p
   JOIN `${dataset}.full2m_eval_query_terms_v01` AS q ON q.term_key=p.term_key
   WHERE p.term_key IN
       (SELECT b.term_key AS term_key
        FROM batch_terms AS b)
   GROUP BY
     q.query_key,
     p.historical_key),
  best_per_query AS
  (SELECT
     c.query_key AS query_key,
     ARRAY_AGG(STRUCT(c.historical_key AS historical_key, c.bm25_score_exact AS bm25_score_exact)
               ORDER BY c.bm25_score_exact DESC, c.historical_key
               LIMIT 20) AS best_matches
   FROM candidate_scores AS c
   GROUP BY c.query_key),
  compact_matches AS
  (SELECT
     b.query_key AS query_key,
     x.historical_key AS historical_key,
     x.bm25_score_exact AS bm25_score_exact,
     rank_offset+1 AS bm25_rank
   FROM best_per_query AS b
   CROSS JOIN UNNEST(b.best_matches) AS x WITH
   OFFSET AS rank_offset)
SELECT
  q.query_document_id AS query_document_id,
  q.query_request_id AS query_request_id,
  h.historical_document_id AS historical_document_id,
  m.request_id AS matched_request_id,
  c.bm25_score_exact AS bm25_score_exact,
  CAST(c.bm25_score_exact AS FLOAT64) AS bm25_score,
  c.bm25_rank AS bm25_rank
FROM compact_matches AS c
JOIN `${dataset}.full2m_eval_queries_v01` AS q ON q.query_key=c.query_key
JOIN `${bm25_history}` AS h ON h.historical_key=c.historical_key
JOIN `${dense_documents}` AS m ON m.document_id=h.historical_document_id;

END IF;
