-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_status AS WITH
  history_summary AS
  (SELECT
     COUNT(*) AS historical_documents,
     COUNT(DISTINCT m.document_id) AS distinct_historical_documents,
     COUNT(DISTINCT m.request_id) AS distinct_historical_inputs,
     MIN(m.corpus_rank) AS minimum_corpus_rank,
     MAX(m.corpus_rank) AS maximum_corpus_rank,
     COUNTIF(p.eligible IS DISTINCT
             FROM TRUE
             OR p.price_observations IS DISTINCT
             FROM 21
             OR ARRAY_LENGTH(p.forward_path) IS DISTINCT
             FROM 21
             OR p.day20_date IS NULL
             OR p.day20_date>=DATE(@window_start)
             OR c.observed_day IS NULL
             OR c.observed_day>=DATE(@window_start)
             OR c.market_anchor_date IS DISTINCT
             FROM p.anchor_date
             OR c.market_day20_date IS DISTINCT
             FROM p.day20_date) AS market_maturity_errors,
     COUNTIF(c.document_id IS NULL
             OR c.metadata_sha256 IS DISTINCT
             FROM m.metadata_sha256
             OR c.corpus_rank IS DISTINCT
             FROM m.corpus_rank
             OR c.observed_day IS DISTINCT
             FROM m.observed_day
             OR m.request_id IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', m.metadata_sha256))))) AS document_binding_errors,
     COUNTIF(v.request_id IS NULL) AS missing_base_inputs
   FROM `${dense_documents}` AS m
   LEFT JOIN `${corpus_documents}` AS c ON c.document_id=m.document_id
   LEFT JOIN `${market_paths}` AS p ON p.observed_day=c.observed_day
   LEFT JOIN `${dense_vectors}` AS v ON v.request_id=m.request_id),
  query_summary AS
  (SELECT
     COUNT(*) AS query_documents,
     COUNT(DISTINCT q.query_document_id) AS distinct_query_documents,
     COUNT(DISTINCT q.query_request_id) AS query_inputs,
     COUNTIF(q.query_document_id IS NULL
             OR q.query_document_id=''
             OR q.query_rank IS NULL
             OR q.query_rank NOT BETWEEN 1 AND 500
             OR q.observed_at IS NULL
             OR q.observed_at<@window_start
             OR q.observed_at>=TIMESTAMP_ADD(@window_start,INTERVAL 1 HOUR)
             OR q.metadata_sha256 IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(q.metadata_text)))
             OR q.metadata_text IS NULL
             OR BYTE_LENGTH(q.metadata_text) NOT BETWEEN 1 AND 6000
             OR q.lex_tokens IS NULL
             OR ARRAY_LENGTH(q.lex_tokens)=0
             OR EXISTS
               (SELECT 1
                FROM UNNEST(q.lex_tokens) AS token
                WHERE token IS NULL
                  OR token='')
             OR r.request_id IS NULL
             OR r.metadata_sha256 IS DISTINCT
             FROM q.metadata_sha256
             OR r.model_input IS DISTINCT
             FROM q.metadata_text
             OR v.query_request_id IS NULL
             OR q.query_request_id IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', q.metadata_sha256))))) AS query_binding_errors
   FROM `${query_documents}` AS q
   LEFT JOIN `${query_requests}` AS r ON r.request_id=q.query_request_id
   LEFT JOIN `${query_vectors}` AS v ON v.query_request_id=q.query_request_id),
  uses AS
  (SELECT
     q.query_request_id AS query_request_id,
     COUNT(*) AS document_uses
   FROM `${query_documents}` AS q
   GROUP BY q.query_request_id),
  vector_summary AS
  (SELECT
     COUNT(*) AS query_vector_rows,
     COUNT(DISTINCT v.query_request_id) AS distinct_query_vectors,
     SUM(v.document_uses) AS query_document_uses,
     COUNTIF(v.query_request_id IS NULL
             OR u.query_request_id IS NULL
             OR v.document_uses IS DISTINCT
             FROM u.document_uses
             OR ARRAY_LENGTH(v.embedding) IS DISTINCT
             FROM 3072
             OR EXISTS
               (SELECT 1
                FROM UNNEST(v.embedding) AS x
                WHERE x IS NULL
                  OR IS_NAN(x)
                  OR IS_INF(x))
             OR
               (SELECT MAX(ABS(x))
                FROM UNNEST(v.embedding) AS x) IS NULL
             OR
               (SELECT MAX(ABS(x))
                FROM UNNEST(v.embedding) AS x)<=0) AS query_vector_errors
   FROM `${query_vectors}` AS v
   LEFT JOIN uses AS u ON u.query_request_id=v.query_request_id),
  lexical_summary AS
  (SELECT
     COUNT(*) AS lexical_history_rows,
     COUNT(DISTINCT h.historical_document_id) AS lexical_history_identities,
     COUNTIF(c.document_id IS NULL
             OR h.historical_key IS NULL
             OR h.historical_key NOT BETWEEN 1 AND @expected_history_documents) AS lexical_mapping_errors
   FROM `${bm25_history}` AS h
   LEFT JOIN `${corpus_documents}` AS c ON c.document_id=h.historical_document_id)
SELECT
  h.historical_documents AS historical_documents,
  h.distinct_historical_documents AS distinct_historical_documents,
  h.distinct_historical_inputs AS distinct_historical_inputs,
  h.minimum_corpus_rank AS minimum_corpus_rank,
  h.maximum_corpus_rank AS maximum_corpus_rank,
  h.market_maturity_errors AS market_maturity_errors,
  h.document_binding_errors AS document_binding_errors,
  h.missing_base_inputs AS missing_base_inputs,
  q.query_documents AS query_documents,
  q.distinct_query_documents AS distinct_query_documents,
  q.query_inputs AS query_inputs,
  q.query_binding_errors AS query_binding_errors,
  v.query_vector_rows AS query_vector_rows,
  v.distinct_query_vectors AS distinct_query_vectors,
  v.query_document_uses AS query_document_uses,
  v.query_vector_errors AS query_vector_errors,

  (SELECT COUNT(*)
   FROM `${query_documents}` AS q
   JOIN `${corpus_documents}` AS c ON c.document_id=q.query_document_id) AS query_history_overlaps,
  l.lexical_history_rows AS lexical_history_rows,
  l.lexical_history_identities AS lexical_history_identities,

  (SELECT COUNT(*)
   FROM `${bm25_terms}`) AS lexical_terms,

  (SELECT COUNT(*)
   FROM `${bm25_weights}`) AS lexical_weight_rows,
  l.lexical_mapping_errors AS lexical_mapping_errors
FROM history_summary AS h
CROSS JOIN query_summary AS q
CROSS JOIN vector_summary AS v
CROSS JOIN lexical_summary AS l
LIMIT 1;

ASSERT
  (SELECT historical_documents=@expected_history_documents
   AND distinct_historical_documents=historical_documents
   AND (@expected_history_inputs IS NULL
        OR distinct_historical_inputs=@expected_history_inputs)
   AND minimum_corpus_rank=1
   AND maximum_corpus_rank=historical_documents
   AND market_maturity_errors=0
   AND document_binding_errors=0
   AND missing_base_inputs=0
   AND query_documents>0
   AND (@expected_query_documents IS NULL
        OR query_documents=@expected_query_documents)
   AND distinct_query_documents=query_documents
   AND
     (SELECT COUNT(DISTINCT query_rank)
      FROM `${query_documents}`)=query_documents
   AND (@expected_query_inputs IS NULL
        OR query_inputs=@expected_query_inputs)
   AND query_binding_errors=0
   AND query_vector_rows=query_inputs
   AND distinct_query_vectors=query_inputs
   AND query_document_uses=query_documents
   AND query_vector_errors=0
   AND (@exclude_identical_inputs
        OR query_history_overlaps=0)
   AND lexical_history_rows=historical_documents
   AND lexical_history_identities=historical_documents
   AND lexical_terms>0
   AND (@expected_lexical_terms IS NULL
        OR lexical_terms=@expected_lexical_terms)
   AND lexical_weight_rows>0
   AND (@expected_lexical_weight_rows IS NULL
        OR lexical_weight_rows=@expected_lexical_weight_rows)
   AND lexical_mapping_errors=0
   FROM evaluation_status) AS 'Saved corpus, complete market paths, lexical mappings and unchanged query inputs are required';


SELECT
  historical_documents,
  distinct_historical_documents,
  distinct_historical_inputs,
  minimum_corpus_rank,
  maximum_corpus_rank,
  market_maturity_errors,
  document_binding_errors,
  missing_base_inputs,
  query_documents,
  distinct_query_documents,
  query_inputs,
  query_binding_errors,
  query_vector_rows,
  distinct_query_vectors,
  query_document_uses,
  query_vector_errors,
  query_history_overlaps,
  lexical_history_rows,
  lexical_history_identities,
  lexical_terms,
  lexical_weight_rows,
  lexical_mapping_errors
FROM evaluation_status
LIMIT 1;
