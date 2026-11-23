-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_status AS WITH
  counts AS
  (SELECT
     m.query_document_id AS query_document_id,
     COUNT(*) AS pairs,
     COUNT(DISTINCT m.historical_document_id) AS distinct_pairs,
     COUNT(DISTINCT m.bm25_rank) AS distinct_ranks
   FROM `${dataset}.full2m_eval_bm25_matches_v01` AS m
   GROUP BY m.query_document_id),
  ordered AS
  (SELECT
     m.query_document_id AS query_document_id,
     m.historical_document_id AS historical_document_id,
     m.bm25_score_exact AS score,
     LAG(m.bm25_score_exact) OVER(PARTITION BY m.query_document_id
                                  ORDER BY m.bm25_rank) AS previous_score,
     LAG(m.historical_document_id) OVER(PARTITION BY m.query_document_id
                                        ORDER BY m.bm25_rank) AS previous_document
   FROM `${dataset}.full2m_eval_bm25_matches_v01` AS m)
SELECT
  COUNT(*) AS match_pairs,
  COUNT(DISTINCT m.query_document_id) AS query_documents_with_matches,

  (SELECT COUNTIF(c.query_document_id IS NULL)
   FROM `${dataset}.full2m_eval_queries_v01` AS q
   LEFT JOIN counts AS c ON c.query_document_id=q.query_document_id) AS query_documents_without_matches,

  (SELECT COUNTIF(c.pairs!=20
                  OR c.distinct_ranks!=20)
   FROM counts AS c) AS queries_with_wrong_count,

  (SELECT COALESCE(SUM(c.pairs-c.distinct_pairs), 0)
   FROM counts AS c) AS duplicate_pairs,
  COUNTIF(m.bm25_score IS NULL
          OR m.bm25_score<=0
          OR IS_NAN(m.bm25_score)
          OR IS_INF(m.bm25_score)) AS invalid_scores,
  COUNTIF(m.bm25_score_exact IS NULL
          OR m.bm25_score_exact<=0) AS invalid_exact_scores,
  COUNTIF(m.bm25_score IS DISTINCT
          FROM CAST(m.bm25_score_exact AS FLOAT64)) AS display_score_errors,
  COUNTIF(m.bm25_rank IS NULL
          OR m.bm25_rank NOT BETWEEN 1 AND 20) AS invalid_ranks,
  COUNTIF(q.query_document_id IS NULL) AS unexpected_queries,
  COUNTIF(h.historical_key IS NULL) AS unexpected_history,
  COUNTIF(m.query_document_id=m.historical_document_id) AS self_matches,

  (SELECT COUNTIF(o.previous_score<o.score
                  OR (o.previous_score=o.score
                      AND o.previous_document>=o.historical_document_id))
   FROM ordered AS o) AS ordering_errors,
  COUNTIF(m.query_request_id=m.matched_request_id) AS identical_input_matches,
  COUNTIF(m.query_request_id IS DISTINCT
          FROM q.query_request_id
          OR m.matched_request_id IS DISTINCT
          FROM d.request_id) AS request_binding_errors
FROM `${dataset}.full2m_eval_bm25_matches_v01` AS m
LEFT JOIN `${dataset}.full2m_eval_queries_v01` AS q ON q.query_document_id=m.query_document_id
LEFT JOIN `${bm25_history}` AS h ON h.historical_document_id=m.historical_document_id
LEFT JOIN `${dense_documents}` AS d ON d.document_id=m.historical_document_id
LIMIT 1;

ASSERT
  (SELECT query_documents_with_matches=
     (SELECT COUNT(*)
      FROM `${query_documents}`)
   AND query_documents_with_matches>0
   AND match_pairs=20*query_documents_with_matches
   AND query_documents_without_matches=0
   AND queries_with_wrong_count=0
   AND duplicate_pairs=0
   AND invalid_scores=0
   AND invalid_exact_scores=0
   AND display_score_errors=0
   AND invalid_ranks=0
   AND unexpected_queries=0
   AND unexpected_history=0
   AND self_matches=0
   AND ordering_errors=0
   AND request_binding_errors=0
   AND (NOT @exclude_identical_inputs
        OR identical_input_matches=0)
   FROM evaluation_status) AS 'Stable lexical results must cover every query with deterministic exact-decimal ranks and valid exclusions';


SELECT
  match_pairs,
  query_documents_with_matches,
  query_documents_without_matches,
  queries_with_wrong_count,
  duplicate_pairs,
  invalid_scores,
  invalid_exact_scores,
  display_score_errors,
  invalid_ranks,
  unexpected_queries,
  unexpected_history,
  self_matches,
  ordering_errors,
  identical_input_matches,
  request_binding_errors
FROM evaluation_status
LIMIT 1;
