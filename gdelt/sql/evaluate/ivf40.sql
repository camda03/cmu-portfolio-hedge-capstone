-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.
ASSERT (@request_k=IF(@exclude_identical_inputs, 22, 20)) AS 'Retained baseline searches 20 request neighbors; filtered mode searches 22 before exclusions';


CREATE TABLE `${dataset}.full2m_eval_ivf40_request_matches_v01` CLUSTER BY query_request_id AS
SELECT
  query.query_request_id AS query_request_id,
  base.request_id AS matched_request_id,
  distance AS cosine_distance
FROM VECTOR_SEARCH(TABLE `${dense_vectors}`, 'embedding',
                     (SELECT q.query_request_id AS query_request_id, q.embedding AS embedding
                      FROM `${query_vectors}` AS q
                      WHERE q.query_request_id IN
                          (SELECT query_request_id
                           FROM `${query_documents}`)), 'embedding', top_k=>@request_k, distance_type=>'COSINE', options=>'{"fraction_lists_to_search":0.4}');


CREATE TABLE `${dataset}.full2m_eval_ivf40_matches_v01` CLUSTER BY query_document_id AS WITH expanded AS
  (SELECT
     q.query_document_id AS query_document_id,
     q.query_request_id AS query_request_id,
     h.document_id AS historical_document_id,
     n.matched_request_id AS matched_request_id,
     n.cosine_distance AS cosine_distance,
     ROW_NUMBER() OVER(PARTITION BY q.query_document_id
                       ORDER BY n.cosine_distance, h.document_id) AS cosine_rank
   FROM `${dataset}.full2m_eval_ivf40_request_matches_v01` AS n
   JOIN `${query_documents}` AS q ON q.query_request_id=n.query_request_id
   JOIN `${dense_documents}` AS h ON h.request_id=n.matched_request_id
   WHERE h.document_id!=q.query_document_id
     AND (NOT @exclude_identical_inputs
          OR n.matched_request_id!=q.query_request_id))
SELECT
  e.query_document_id AS query_document_id,
  e.query_request_id AS query_request_id,
  e.historical_document_id AS historical_document_id,
  e.matched_request_id AS matched_request_id,
  e.cosine_distance AS cosine_distance,
  1.0-e.cosine_distance AS cosine_similarity,
  e.cosine_rank AS cosine_rank
FROM expanded AS e
WHERE e.cosine_rank<=20;
