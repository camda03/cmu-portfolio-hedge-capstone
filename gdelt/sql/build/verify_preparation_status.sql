CREATE TEMP TABLE validation_control AS WITH
  raw_vectors AS
  (SELECT
     v.query_request_id AS request_id,
     0 AS batch_number,
     '' AS status,
     v.embedding AS embedding
   FROM `${dataset}.filtered_verify_query_vectors_v01` AS v),
  raw_checks AS
  (SELECT
     r.request_id AS request_id,
     r.batch_number AS batch_number,
     r.status AS status,
     r.embedding AS embedding,
     ARRAY_LENGTH(r.embedding) AS dimensions,

     (SELECT COUNTIF(x IS NULL
                     OR IS_NAN(x)
                     OR IS_INF(x))
      FROM UNNEST(r.embedding) AS x) AS invalid_components,

     (SELECT MAX(ABS(x))
      FROM UNNEST(r.embedding) AS x) AS scale
   FROM raw_vectors AS r),
  scaled_norms AS
  (SELECT
     r.request_id AS request_id,
     r.batch_number AS batch_number,
     r.status AS status,
     r.embedding AS embedding,
     r.dimensions AS dimensions,
     r.invalid_components AS invalid_components,
     IF(r.invalid_components=0
        AND r.scale>0
        AND NOT IS_NAN(r.scale)
        AND NOT IS_INF(r.scale), SAFE_MULTIPLY(r.scale, SQRT(
                                                               (SELECT SUM(POW(SAFE_DIVIDE(x, r.scale), 2))
                                                                FROM UNNEST(r.embedding) AS x))), NULL) AS vector_norm
   FROM raw_checks AS r),
  validated AS
  (SELECT
     n.request_id AS request_id,
     n.batch_number AS batch_number,
     n.status AS status,
     n.dimensions AS dimensions,
     n.invalid_components AS invalid_components,
     n.vector_norm AS vector_norm,
     COALESCE(n.status=''
              AND n.dimensions=3072
              AND n.invalid_components=0
              AND n.vector_norm>0
              AND NOT IS_NAN(n.vector_norm)
              AND NOT IS_INF(n.vector_norm), FALSE) AS valid,
     LOWER(TO_HEX(SHA256(TO_JSON_STRING(n.embedding)))) AS vector_sha256
   FROM scaled_norms AS n),
  documents AS
  (SELECT
     COUNT(*) AS query_documents,
     COUNT(DISTINCT q.query_document_id) AS distinct_query_documents,
     COUNT(DISTINCT q.query_request_id) AS requested_inputs,
     COUNTIF(q.metadata_text IS NULL
             OR q.metadata_text=''
             OR BYTE_LENGTH(q.metadata_text)>6000
             OR q.metadata_sha256 IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(q.metadata_text)))
             OR q.query_request_id IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', q.metadata_sha256))))
             OR ARRAY_LENGTH(q.lex_tokens) IS NULL
             OR ARRAY_LENGTH(q.lex_tokens)=0
             OR ARRAY_LENGTH(q.matched_rule_ids) IS NULL
             OR ARRAY_LENGTH(q.matched_rule_ids)=0
             OR q.observed_at<TIMESTAMP '2026-10-30T23:00:00+00:00'
             OR q.observed_at>=TIMESTAMP '2026-10-31T00:00:00+00:00'
             OR q.source_revision_key IS NULL
             OR q.raw_revision_sha256 IS NULL) AS query_binding_errors
   FROM `${dataset}.filtered_verify_query_documents_v01` AS q),
  vectors AS
  (SELECT
     COUNT(*) AS query_inputs,
     COUNT(DISTINCT v.request_id) AS distinct_query_inputs,
     COUNTIF(NOT v.valid) AS vector_errors
   FROM validated AS v),
  missing AS
  (SELECT COUNTIF(v.query_request_id IS NULL) AS missing_query_vectors
   FROM `${dataset}.filtered_verify_query_documents_v01` AS q
   LEFT JOIN `${dataset}.filtered_verify_query_vectors_v01` AS v ON v.query_request_id=q.query_request_id),
  references AS
  (SELECT
     COUNT(*) AS reference_documents,
     COUNT(DISTINCT r.query_request_id) AS distinct_reference_requests,
     COUNTIF(q.query_document_id IS NULL
             OR q.query_request_id IS DISTINCT
             FROM r.query_request_id) AS reference_errors
   FROM `${dataset}.filtered_verify_reference_documents_v01` AS r
   LEFT JOIN `${dataset}.filtered_verify_query_documents_v01` AS q ON q.query_document_id=r.query_document_id),
  lexical AS
  (SELECT
     (SELECT COUNTIF(q.query_document_id IS NULL
                     OR l.query_document_id IS NULL
                     OR q.query_request_id IS DISTINCT
                     FROM l.query_request_id
                     OR l.lexical_terms_json IS DISTINCT
                     FROM TO_JSON_STRING(ARRAY
                                           (SELECT DISTINCT token
                                            FROM UNNEST(q.lex_tokens) AS token
                                            ORDER BY token)))
      FROM `${dataset}.filtered_verify_query_documents_v01` AS q
      FULL OUTER JOIN `${dataset}.filtered_verify_queries_v01` AS l ON l.query_document_id=q.query_document_id) +
     (SELECT COUNT(*)
      FROM
        (SELECT
           t.query_key,
           t.term_key,
           COUNT(*) AS pairs
         FROM `${dataset}.filtered_verify_query_terms_v01` AS t
         GROUP BY
           t.query_key,
           t.term_key
         HAVING COUNT(*)!=1)) AS lexical_mapping_errors),
  history AS
  (SELECT COUNTIF(h.market_day20_date IS NULL
                  OR h.market_day20_date>=DATE '2026-10-30') AS history_maturity_errors
   FROM `${dataset}.corpus_documents` AS h),
  manifest AS
  (SELECT IF(COUNT(*)=1
             AND COUNTIF(m.release='gkg-filtered-2m-verification-v01-r01'
                         AND m.contract_sha256=@contract_sha256
                         AND m.profile='expanded'
                         AND m.rules_sha256='ed6f55b8ee8450f5faedb009d7f4ff1854d4707c5f0c8ab59755712d5b254b77'
                         AND m.embedding_spec_sha256='b935834ce400c38516af947df67c356c90f67bac60e0feb4e7ee4760c302e9f8'
                         AND m.saved_source_documents=500
                         AND m.qualifying_query_documents=d.query_documents
                         AND m.query_inputs=v.query_inputs
                         AND m.reference_documents=r.reference_documents
                         AND m.maturity_cutoff=DATE '2026-10-30'
                         AND m.source_window_start=TIMESTAMP '2026-10-30T23:00:00+00:00'
                         AND m.source_window_end=TIMESTAMP '2026-10-31T00:00:00+00:00'
                         AND m.saved_source_as_of=TIMESTAMP '2026-10-31T00:29:01.596982+00:00')=1, 0, 1) AS manifest_errors
   FROM `${dataset}.filtered_verify_manifest_v01` AS m
   CROSS JOIN documents AS d
   CROSS JOIN vectors AS v
   CROSS JOIN references AS r)
SELECT
  d.query_documents AS query_documents,
  d.distinct_query_documents AS distinct_query_documents,
  v.query_inputs AS query_inputs,
  v.distinct_query_inputs AS distinct_query_inputs,
  r.reference_documents AS reference_documents,
  r.distinct_reference_requests AS distinct_reference_requests,
  d.query_binding_errors AS query_binding_errors,
  v.vector_errors AS vector_errors,
  x.missing_query_vectors AS missing_query_vectors,
  r.reference_errors AS reference_errors,
  l.lexical_mapping_errors AS lexical_mapping_errors,
  h.history_maturity_errors AS history_maturity_errors,
  m.manifest_errors AS manifest_errors,
  IF(d.query_documents BETWEEN 1 AND 500
     AND d.query_documents=d.distinct_query_documents
     AND d.query_binding_errors=0
     AND v.query_inputs=d.requested_inputs
     AND v.query_inputs=v.distinct_query_inputs
     AND v.vector_errors=0
     AND x.missing_query_vectors=0
     AND r.reference_documents=LEAST(32, v.query_inputs)
     AND r.distinct_reference_requests=r.reference_documents
     AND r.reference_errors=0
     AND l.lexical_mapping_errors=0
     AND h.history_maturity_errors=0
     AND m.manifest_errors=0, 0, 1) AS preparation_contract_errors
FROM documents AS d
CROSS JOIN vectors AS v
CROSS JOIN missing AS x
CROSS JOIN references AS r
CROSS JOIN lexical AS l
CROSS JOIN history AS h
CROSS JOIN manifest AS m
LIMIT 1;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF((preparation_contract_errors=0) IS DISTINCT
               FROM TRUE)=0
   FROM validation_control) AS 'verify_preparation_status: frozen contract failed; retain the original job for recovery';


SELECT
  query_documents,
  distinct_query_documents,
  query_inputs,
  distinct_query_inputs,
  reference_documents,
  distinct_reference_requests,
  query_binding_errors,
  vector_errors,
  missing_query_vectors,
  reference_errors,
  lexical_mapping_errors,
  history_maturity_errors,
  manifest_errors,
  preparation_contract_errors
FROM validation_control;
