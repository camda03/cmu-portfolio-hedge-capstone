-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_status AS WITH
  counts AS
  (SELECT
     m.query_document_id AS query_document_id,
     COUNT(*) AS pairs,
     COUNT(DISTINCT m.historical_document_id) AS distinct_pairs,
     COUNT(DISTINCT m.cosine_rank) AS distinct_ranks,
     LOWER(TO_HEX(SHA256(STRING_AGG(m.historical_document_id, '\n'
                                    ORDER BY m.historical_document_id)))) AS candidate_set
   FROM `${dataset}.full2m_eval_exact_matches_v01` AS m
   GROUP BY m.query_document_id),
  ordered AS
  (SELECT
     m.query_document_id AS query_document_id,
     m.historical_document_id AS historical_document_id,
     m.cosine_distance AS distance,
     LAG(m.cosine_distance) OVER(PARTITION BY m.query_document_id
                                 ORDER BY m.cosine_rank) AS previous_distance,
     LAG(m.historical_document_id) OVER(PARTITION BY m.query_document_id
                                        ORDER BY m.cosine_rank) AS previous_document
   FROM `${dataset}.full2m_eval_exact_matches_v01` AS m)
SELECT

  (SELECT COUNT(DISTINCT q.query_request_id)
   FROM `${dataset}.full2m_eval_reference_documents_v01` AS q) AS query_requests,

  (SELECT COUNT(*)
   FROM `${dataset}.full2m_eval_exact_request_matches_v01`) AS request_pairs,

  (SELECT COUNT(*)
   FROM `${dataset}.full2m_eval_exact_matches_v01`) AS document_pairs,

  (SELECT COUNT(*)
   FROM counts) AS query_documents,

  (SELECT COALESCE(SUM(c.pairs-c.distinct_pairs), 0)
   FROM counts AS c) AS duplicate_document_pairs,

  (SELECT COUNT(*)-COUNT(DISTINCT TO_JSON_STRING(STRUCT(r.query_request_id AS query_request_id, r.matched_request_id AS matched_request_id)))
   FROM `${dataset}.full2m_eval_exact_request_matches_v01` AS r) AS duplicate_request_pairs,

  (SELECT COUNTIF(c.pairs!=20
                  OR c.distinct_ranks!=20)
   FROM counts AS c) AS queries_with_wrong_count,

  (SELECT COUNTIF(c.query_document_id IS NULL)
   FROM `${dataset}.full2m_eval_reference_documents_v01` AS q
   LEFT JOIN counts AS c ON c.query_document_id=q.query_document_id) AS missing_queries,
  COUNTIF(q.query_document_id IS NULL) AS unexpected_queries,
  COUNTIF(h.document_id IS NULL) AS unexpected_history,
  COUNTIF(m.query_document_id=m.historical_document_id) AS self_matches,
  COUNTIF(m.cosine_distance IS NULL
          OR IS_NAN(m.cosine_distance)
          OR IS_INF(m.cosine_distance)
          OR m.cosine_distance< -1e-8
          OR m.cosine_distance>2.00000001
          OR m.cosine_similarity IS DISTINCT
          FROM 1.0-m.cosine_distance) AS invalid_distances,
  COUNTIF(m.cosine_rank IS NULL
          OR m.cosine_rank NOT BETWEEN 1 AND 20) AS invalid_ranks,

  (SELECT COUNTIF(o.previous_distance>o.distance
                  OR (o.previous_distance=o.distance
                      AND o.previous_document>=o.historical_document_id))
   FROM ordered AS o) AS ordering_errors,

  (SELECT COUNT(DISTINCT c.candidate_set)
   FROM counts AS c) AS distinct_candidate_sets,
  COUNTIF(m.query_request_id=m.matched_request_id) AS identical_input_matches,
  COUNTIF(m.query_request_id IS DISTINCT
          FROM q.query_request_id
          OR m.matched_request_id IS DISTINCT
          FROM h.request_id) AS request_binding_errors
FROM `${dataset}.full2m_eval_exact_matches_v01` AS m
LEFT JOIN `${dataset}.full2m_eval_reference_documents_v01` AS q ON q.query_document_id=m.query_document_id
LEFT JOIN `${dense_documents}` AS h ON h.document_id=m.historical_document_id
LIMIT 1;

ASSERT
  (SELECT query_documents=
     (SELECT COUNT(*)
      FROM `${dataset}.full2m_eval_reference_documents_v01`)
   AND query_documents>0
   AND document_pairs=20*query_documents
   AND request_pairs=@request_k*query_requests
   AND duplicate_document_pairs=0
   AND duplicate_request_pairs=0
   AND queries_with_wrong_count=0
   AND missing_queries=0
   AND unexpected_queries=0
   AND unexpected_history=0
   AND self_matches=0
   AND invalid_distances=0
   AND invalid_ranks=0
   AND ordering_errors=0
   AND request_binding_errors=0
   AND (NOT @exclude_identical_inputs
        OR identical_input_matches=0)
   FROM evaluation_status) AS 'Cosine must cover the selected query population with deterministic document ranks and valid exclusions';


SELECT
  query_requests,
  request_pairs,
  document_pairs,
  query_documents,
  duplicate_document_pairs,
  duplicate_request_pairs,
  queries_with_wrong_count,
  missing_queries,
  unexpected_queries,
  unexpected_history,
  self_matches,
  invalid_distances,
  invalid_ranks,
  ordering_errors,
  distinct_candidate_sets,
  identical_input_matches,
  request_binding_errors
FROM evaluation_status
LIMIT 1;
