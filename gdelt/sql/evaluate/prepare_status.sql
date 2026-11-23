-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_status AS WITH
  expected_terms AS
  (SELECT DISTINCT
     q.query_key AS query_key,
     t.term_key AS term_key
   FROM `${dataset}.full2m_eval_queries_v01` AS q
   CROSS JOIN UNNEST(JSON_VALUE_ARRAY(q.lexical_terms_json)) AS token
   JOIN `${bm25_terms}` AS t ON t.term=token),
  reference_expected AS
  (SELECT
     q.query_document_id AS query_document_id,
     q.query_rank AS query_rank,
     q.query_request_id AS query_request_id
   FROM `${query_documents}` AS q QUALIFY ROW_NUMBER() OVER(PARTITION BY q.query_request_id
                                                            ORDER BY q.query_rank, q.query_document_id)=1
   ORDER BY
     SHA256(CONCAT(@reference_seed, '\n', q.query_request_id)),
     q.query_request_id
   LIMIT 32)
SELECT

  (SELECT COUNT(*)
   FROM `${dataset}.full2m_eval_queries_v01`) AS query_documents,

  (SELECT COUNT(DISTINCT q.query_document_id)
   FROM `${dataset}.full2m_eval_queries_v01` AS q) AS distinct_query_documents,

  (SELECT COUNT(DISTINCT q.query_key)
   FROM `${dataset}.full2m_eval_queries_v01` AS q) AS unique_lexical_queries,

  (SELECT COUNT(*)
   FROM `${dataset}.full2m_eval_query_terms_v01`) AS query_term_pairs,

  (SELECT COUNT(DISTINCT TO_JSON_STRING(STRUCT(q.query_key AS query_key, q.term_key AS term_key)))
   FROM `${dataset}.full2m_eval_query_terms_v01` AS q) AS distinct_query_term_pairs,

  (SELECT COUNTIF(q.query_document_id IS NULL
                  OR s.query_document_id IS NULL
                  OR q.query_key IS NULL
                  OR q.query_key<=0
                  OR q.query_request_id IS DISTINCT
                  FROM s.query_request_id
                  OR q.lexical_terms_json IS DISTINCT
                  FROM TO_JSON_STRING(ARRAY
                                        (SELECT DISTINCT token
                                         FROM UNNEST(s.lex_tokens) AS token
                                         ORDER BY token)))
   FROM `${dataset}.full2m_eval_queries_v01` AS q
   FULL OUTER JOIN `${query_documents}` AS s ON s.query_document_id=q.query_document_id) +
  (SELECT COUNT(*)
   FROM
     (SELECT q.query_key AS query_key
      FROM `${dataset}.full2m_eval_queries_v01` AS q
      GROUP BY q.query_key
      HAVING COUNT(DISTINCT q.lexical_terms_json)!=1)) +
  (SELECT COUNT(*)
   FROM
     (SELECT q.lexical_terms_json AS lexical_terms_json
      FROM `${dataset}.full2m_eval_queries_v01` AS q
      GROUP BY q.lexical_terms_json
      HAVING COUNT(DISTINCT q.query_key)!=1)) AS query_binding_errors,

  (SELECT COUNTIF(e.query_key IS NULL
                  OR a.query_key IS NULL)
   FROM expected_terms AS e
   FULL OUTER JOIN `${dataset}.full2m_eval_query_terms_v01` AS a ON a.query_key=e.query_key
   AND a.term_key=e.term_key) AS term_binding_errors,

  (SELECT COUNT(*)
   FROM `${dataset}.full2m_eval_reference_documents_v01`) AS reference_documents,

  (SELECT COUNT(DISTINCT r.query_document_id)
   FROM `${dataset}.full2m_eval_reference_documents_v01` AS r) AS reference_distinct_documents,

  (SELECT COUNT(DISTINCT r.query_request_id)
   FROM `${dataset}.full2m_eval_reference_documents_v01` AS r) AS reference_unique_inputs,

  (SELECT COUNTIF(e.query_document_id IS NULL
                  OR a.query_document_id IS NULL
                  OR a.query_request_id IS DISTINCT
                  FROM e.query_request_id
                  OR a.query_rank IS DISTINCT
                  FROM e.query_rank)
   FROM reference_expected AS e
   FULL OUTER JOIN `${dataset}.full2m_eval_reference_documents_v01` AS a ON a.query_document_id=e.query_document_id) AS reference_binding_errors
FROM UNNEST([1]) AS singleton
LIMIT 1;

ASSERT
  (SELECT query_documents>0
   AND query_documents=
     (SELECT COUNT(*)
      FROM `${query_documents}`)
   AND distinct_query_documents=query_documents
   AND query_term_pairs=distinct_query_term_pairs
   AND query_binding_errors=0
   AND term_binding_errors=0
   AND reference_binding_errors=0
   AND reference_documents=LEAST(32,
                                   (SELECT COUNT(DISTINCT query_request_id)
                                    FROM `${query_documents}`))
   AND reference_distinct_documents=reference_documents
   AND reference_unique_inputs=reference_documents
   FROM evaluation_status) AS 'Query identities, lexical mappings and exact reference must match the saved input cohort';


SELECT
  query_documents,
  distinct_query_documents,
  unique_lexical_queries,
  query_term_pairs,
  distinct_query_term_pairs,
  query_binding_errors,
  term_binding_errors,
  reference_documents,
  reference_distinct_documents,
  reference_unique_inputs,
  reference_binding_errors
FROM evaluation_status
LIMIT 1;
