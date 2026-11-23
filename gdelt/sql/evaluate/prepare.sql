-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TABLE `${dataset}.full2m_eval_queries_v01` CLUSTER BY
  query_key,
  query_document_id AS WITH normalized AS
  (SELECT
     q.query_document_id AS query_document_id,
     q.query_request_id AS query_request_id,
     TO_JSON_STRING(ARRAY
                      (SELECT DISTINCT token
                       FROM UNNEST(q.lex_tokens) AS token
                       ORDER BY token)) AS lexical_terms_json
   FROM `${query_documents}` AS q)
SELECT
  q.query_document_id AS query_document_id,
  q.query_request_id AS query_request_id,
  q.lexical_terms_json AS lexical_terms_json,
  DENSE_RANK() OVER(
                    ORDER BY q.lexical_terms_json) AS query_key
FROM normalized AS q;


CREATE TABLE `${dataset}.full2m_eval_query_terms_v01` CLUSTER BY
  term_key,
  query_key AS
SELECT DISTINCT
  q.query_key AS query_key,
  t.term_key AS term_key
FROM `${dataset}.full2m_eval_queries_v01` AS q
CROSS JOIN UNNEST(JSON_VALUE_ARRAY(q.lexical_terms_json)) AS token
JOIN `${bm25_terms}` AS t ON t.term=token;


CREATE TABLE `${dataset}.full2m_eval_reference_documents_v01` AS WITH representatives AS
  (SELECT
     q.query_document_id AS query_document_id,
     q.query_rank AS query_rank,
     q.query_request_id AS query_request_id
   FROM `${query_documents}` AS q QUALIFY ROW_NUMBER() OVER(PARTITION BY q.query_request_id
                                                            ORDER BY q.query_rank, q.query_document_id)=1)
SELECT
  q.query_document_id AS query_document_id,
  q.query_rank AS query_rank,
  q.query_request_id AS query_request_id
FROM representatives AS q
ORDER BY
  SHA256(CONCAT(@reference_seed, '\n', q.query_request_id)),
  q.query_request_id
LIMIT 32;
