-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_status AS WITH
  counts AS
  (SELECT
     m.query_document_id AS query_document_id,
     COUNT(*) AS pairs,
     COUNT(DISTINCT m.historical_document_id) AS distinct_pairs,
     COUNT(DISTINCT m.fused_rank) AS distinct_ranks,
     LOWER(TO_HEX(SHA256(STRING_AGG(m.historical_document_id, '\n'
                                    ORDER BY m.historical_document_id)))) AS candidate_set
   FROM `${dataset}.full2m_eval_matches_v01` AS m
   GROUP BY m.query_document_id),
  ordered AS
  (SELECT
     m.query_document_id AS query_document_id,
     m.historical_document_id AS historical_document_id,
     m.rrf_score AS score,
     LAG(m.rrf_score) OVER(PARTITION BY m.query_document_id
                           ORDER BY m.fused_rank) AS previous_score,
     LAG(m.historical_document_id) OVER(PARTITION BY m.query_document_id
                                        ORDER BY m.fused_rank) AS previous_document
   FROM `${dataset}.full2m_eval_matches_v01` AS m),
  source_pairs AS
  (SELECT
     COALESCE(c.query_document_id, b.query_document_id) AS query_document_id,
     COALESCE(c.historical_document_id, b.historical_document_id) AS historical_document_id,
     c.cosine_rank AS cosine_rank,
     c.cosine_distance AS cosine_distance,
     c.cosine_similarity AS cosine_similarity,
     b.bm25_rank AS bm25_rank,
     b.bm25_score AS bm25_score,
     b.bm25_score_exact AS bm25_score_exact
   FROM `${dataset}.full2m_eval_ivf40_matches_v01` AS c
   FULL OUTER JOIN `${dataset}.full2m_eval_bm25_matches_v01` AS b ON b.query_document_id=c.query_document_id
   AND b.historical_document_id=c.historical_document_id),
  bindings AS
  (SELECT COUNTIF(s.query_document_id IS NULL
                  OR m.query_document_id IS NULL
                  OR m.cosine_rank IS DISTINCT
                  FROM s.cosine_rank
                  OR m.cosine_distance IS DISTINCT
                  FROM s.cosine_distance
                  OR m.cosine_similarity IS DISTINCT
                  FROM s.cosine_similarity
                  OR m.bm25_rank IS DISTINCT
                  FROM s.bm25_rank
                  OR m.bm25_score IS DISTINCT
                  FROM s.bm25_score
                  OR m.bm25_score_exact IS DISTINCT
                  FROM s.bm25_score_exact) AS errors
   FROM source_pairs AS s
   FULL OUTER JOIN `${dataset}.full2m_eval_matches_v01` AS m ON m.query_document_id=s.query_document_id
   AND m.historical_document_id=s.historical_document_id)
SELECT
  COUNT(DISTINCT m.query_document_id) AS completed_query_documents,
  COUNT(*) AS candidate_pairs,
  COUNTIF(m.cosine_rank IS NOT NULL) AS cosine_candidate_pairs,
  COUNTIF(m.bm25_rank IS NOT NULL) AS bm25_candidate_pairs,
  COUNTIF(m.cosine_rank IS NOT NULL
          AND m.bm25_rank IS NOT NULL) AS both_channel_pairs,

  (SELECT COALESCE(SUM(c.pairs-c.distinct_pairs), 0)
   FROM counts AS c) AS duplicate_pairs,
  COUNTIF(m.rrf_score IS NULL
          OR m.rrf_score<=0
          OR IS_NAN(m.rrf_score)
          OR IS_INF(m.rrf_score)
          OR m.rrf_score IS DISTINCT
          FROM IF(m.cosine_rank IS NULL, 0.0, 1.0/(60.0+m.cosine_rank))+IF(m.bm25_rank IS NULL, 0.0, 1.0/(60.0+m.bm25_rank))
          OR m.fused_rank IS NULL
          OR m.fused_rank NOT BETWEEN 1 AND 40) +
  (SELECT COUNTIF(c.pairs NOT BETWEEN 20 AND 40
                  OR c.distinct_ranks!=c.pairs)
   FROM counts AS c) AS invalid_fusion,

  (SELECT COUNTIF(c.query_document_id IS NULL)
   FROM `${query_documents}` AS q
   LEFT JOIN counts AS c ON c.query_document_id=q.query_document_id) AS missing_queries,
  COUNTIF(q.query_document_id IS NULL) AS unexpected_queries,
  COUNTIF(h.document_id IS NULL) AS unexpected_history,

  (SELECT COUNT(DISTINCT c.candidate_set)
   FROM counts AS c) AS distinct_candidate_sets,

  (SELECT b.errors
   FROM bindings AS b) AS channel_binding_errors,

  (SELECT COUNTIF(o.previous_score<o.score
                  OR (o.previous_score=o.score
                      AND o.previous_document>=o.historical_document_id))
   FROM ordered AS o) AS ordering_errors
FROM `${dataset}.full2m_eval_matches_v01` AS m
LEFT JOIN `${query_documents}` AS q ON q.query_document_id=m.query_document_id
LEFT JOIN `${dense_documents}` AS h ON h.document_id=m.historical_document_id
LIMIT 1;

ASSERT
  (SELECT completed_query_documents=
     (SELECT COUNT(*)
      FROM `${query_documents}`)
   AND completed_query_documents>0
   AND cosine_candidate_pairs=20*completed_query_documents
   AND bm25_candidate_pairs=20*completed_query_documents
   AND candidate_pairs=cosine_candidate_pairs+bm25_candidate_pairs-both_channel_pairs
   AND duplicate_pairs=0
   AND invalid_fusion=0
   AND missing_queries=0
   AND unexpected_queries=0
   AND unexpected_history=0
   AND channel_binding_errors=0
   AND ordering_errors=0
   FROM evaluation_status) AS 'Fusion must retain the full cosine and BM25 candidate union with reciprocal rank k=60';


SELECT
  completed_query_documents,
  candidate_pairs,
  cosine_candidate_pairs,
  bm25_candidate_pairs,
  both_channel_pairs,
  duplicate_pairs,
  invalid_fusion,
  missing_queries,
  unexpected_queries,
  unexpected_history,
  distinct_candidate_sets,
  channel_binding_errors,
  ordering_errors
FROM evaluation_status
LIMIT 1;
