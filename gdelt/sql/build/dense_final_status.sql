CREATE TEMP TABLE validation_control AS WITH
  raw_vectors AS
  (SELECT
     v.request_id AS request_id,
     v.batch_number AS batch_number,
     '' AS status,
     v.embedding AS embedding
   FROM `${dataset}.filtered_dense_vectors_v01` AS v),
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
  sources AS
  (SELECT
     k.request_id AS request_id,
     0 AS batch_number,
     k.vector_sha256 AS vector_sha256
   FROM `${dataset}.filtered_dense_cache_v01` AS k
   UNION ALL BY NAME SELECT
     e.request_id AS request_id,
     e.batch_number AS batch_number,
     LOWER(TO_HEX(SHA256(TO_JSON_STRING(e.outcome.result)))) AS vector_sha256
   FROM `${dataset}.filtered_dense_raw_v01` AS e
   WHERE e.batch_number BETWEEN 0 AND 80),
  source_hashes AS
  (SELECT
     s.request_id AS request_id,
     COUNT(*) AS source_rows,
     MIN(s.batch_number) AS batch_number,
     MIN(s.vector_sha256) AS vector_sha256
   FROM sources AS s
   GROUP BY s.request_id),
  norms AS
  (SELECT
     COUNTIF(NOT v.valid) AS invalid_vectors,
     COUNTIF(s.source_rows IS DISTINCT
             FROM 1
             OR s.batch_number IS DISTINCT
             FROM v.batch_number
             OR s.vector_sha256 IS DISTINCT
             FROM v.vector_sha256) AS source_vector_changes
   FROM validated AS v
   LEFT JOIN source_hashes AS s ON s.request_id=v.request_id),
  vectors AS
  (SELECT
     COUNT(*) AS vector_rows,
     COUNT(DISTINCT v.request_id) AS distinct_requests,
     COUNTIF(v.cache_source_kind=1) AS cached_vectors,
     COUNTIF(v.cache_source_kind=0) AS new_vectors,
     COUNTIF(p.request_id IS NULL
             OR p.full_input_rank IS DISTINCT
             FROM v.full_input_rank
             OR p.first_corpus_rank IS DISTINCT
             FROM v.first_corpus_rank
             OR p.batch_number IS DISTINCT
             FROM v.batch_number
             OR p.cache_source_kind IS DISTINCT
             FROM v.cache_source_kind) AS unexpected_vectors
   FROM `${dataset}.filtered_dense_vectors_v01` AS v
   LEFT JOIN `${dataset}.filtered_dense_inputs_v01` AS p ON p.request_id=v.request_id
   AND p.batch_number BETWEEN 0 AND 80),
  missing AS
  (SELECT
     COUNT(*) AS expected_inputs,
     COUNTIF(v.request_id IS NULL) AS missing_vectors
   FROM `${dataset}.filtered_dense_inputs_v01` AS p
   LEFT JOIN `${dataset}.filtered_dense_vectors_v01` AS v ON v.request_id=p.request_id
   WHERE p.batch_number BETWEEN 0 AND 80),
  documents AS
  (SELECT
     COUNT(*) AS document_rows,
     COUNT(DISTINCT m.document_id) AS distinct_documents,
     COUNTIF(v.request_id IS NULL
             OR p.request_id IS NULL
             OR p.metadata_sha256 IS DISTINCT
             FROM m.metadata_sha256
             OR p.first_corpus_rank>m.corpus_rank) AS document_mapping_errors
   FROM `${dataset}.filtered_dense_documents_v01` AS m
   LEFT JOIN `${dataset}.filtered_dense_vectors_v01` AS v ON v.request_id=m.request_id
   LEFT JOIN `${dataset}.filtered_dense_inputs_v01` AS p ON p.request_id=m.request_id
   AND p.batch_number BETWEEN 0 AND 80),
  unused AS
  (SELECT COUNTIF(m.request_id IS NULL) AS unused_vectors
   FROM `${dataset}.filtered_dense_vectors_v01` AS v
   LEFT JOIN
     (SELECT m.request_id AS request_id
      FROM `${dataset}.filtered_dense_documents_v01` AS m
      GROUP BY m.request_id) AS m ON m.request_id=v.request_id),
  raw_totals AS
  (SELECT
     COUNT(*) AS raw_output_rows,
     COUNT(DISTINCT e.request_id) AS raw_distinct_requests
   FROM `${dataset}.filtered_dense_raw_v01` AS e
   WHERE e.batch_number BETWEEN 0 AND 80),
  manifest AS
  (WITH
     input_blocks AS
     (SELECT
        DIV(p.full_input_rank-1, 1000) AS rank_block,
        COUNT(*) AS input_count,
        COUNTIF(p.cache_source_kind=1) AS cached_inputs,
        COUNTIF(p.cache_source_kind=0) AS new_inputs,
        LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(CAST(p.full_input_rank AS STRING), '\t', p.request_id, '\t', p.metadata_sha256, '\t', CAST(p.batch_number AS STRING), '\n'), ''
                                       ORDER BY p.full_input_rank)))) AS block_sha256
      FROM `${dataset}.filtered_dense_inputs_v01` AS p
      WHERE p.batch_number BETWEEN 0 AND 80
      GROUP BY rank_block),
     input_fingerprint AS
     (SELECT
        COALESCE(SUM(b.input_count), 0) AS input_count,
        COALESCE(SUM(b.cached_inputs), 0) AS cached_inputs,
        COALESCE(SUM(b.new_inputs), 0) AS new_inputs,
        LOWER(TO_HEX(SHA256(COALESCE(STRING_AGG(CONCAT(CAST(b.rank_block AS STRING), '\t', b.block_sha256, '\n'), ''
                                                ORDER BY b.rank_block), '')))) AS inputs_sha256
      FROM input_blocks AS b),
     document_blocks AS
     (SELECT
        DIV(m.corpus_rank-1, 1000) AS rank_block,
        COUNT(*) AS document_count,
        LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(CAST(m.corpus_rank AS STRING), '\t', m.document_id, '\t', m.request_id, '\t', CAST(m.observed_day AS STRING), '\n'), ''
                                       ORDER BY m.corpus_rank)))) AS block_sha256
      FROM `${dataset}.filtered_dense_documents_v01` AS m
      GROUP BY rank_block),
     document_fingerprint AS
     (SELECT
        COALESCE(SUM(b.document_count), 0) AS document_count,
        LOWER(TO_HEX(SHA256(COALESCE(STRING_AGG(CONCAT(CAST(b.rank_block AS STRING), '\t', b.block_sha256, '\n'), ''
                                                ORDER BY b.rank_block), '')))) AS documents_sha256
      FROM document_blocks AS b),
     cache_blocks AS
     (SELECT
        SUBSTR(k.request_id, 1, 3) AS hash_block,
        COUNT(*) AS cache_count,
        LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(k.request_id, '\t', k.vector_sha256, '\n'), ''
                                       ORDER BY k.request_id)))) AS block_sha256
      FROM `${dataset}.filtered_dense_cache_v01` AS k
      GROUP BY hash_block),
     cache_fingerprint AS
     (SELECT
        COALESCE(SUM(b.cache_count), 0) AS cache_count,
        LOWER(TO_HEX(SHA256(COALESCE(STRING_AGG(CONCAT(b.hash_block, '\t', b.block_sha256, '\n'), ''
                                                ORDER BY b.hash_block), '')))) AS cache_sha256
      FROM cache_blocks AS b) SELECT IF(COUNT(*)=1
                                        AND COUNTIF(m.contract_sha256=@contract_sha256
                                                    AND m.release='gkg-filtered-2m-dense-v01-r01'
                                                    AND m.embedding_spec_sha256='b935834ce400c38516af947df67c356c90f67bac60e0feb4e7ee4760c302e9f8'
                                                    AND m.embedding_spec_json='{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}'
                                                    AND m.document_count=2000000
                                                    AND m.document_count=d.document_count
                                                    AND m.input_count=i.input_count
                                                    AND m.input_count BETWEEN 1 AND 2000000
                                                    AND m.cached_inputs=i.cached_inputs
                                                    AND m.cached_inputs=k.cache_count
                                                    AND m.new_inputs=i.new_inputs
                                                    AND m.input_count=m.cached_inputs+m.new_inputs
                                                    AND m.batch_size=25000
                                                    AND m.batch_count=DIV(m.new_inputs+25000-1, 25000)
                                                    AND m.inputs_sha256=i.inputs_sha256
                                                    AND m.documents_sha256=d.documents_sha256
                                                    AND m.cache_sha256=k.cache_sha256)=1, 0, 1) AS manifest_errors
   FROM `${dataset}.filtered_dense_manifest_v01` AS m
   CROSS JOIN input_fingerprint AS i
   CROSS JOIN document_fingerprint AS d
   CROSS JOIN cache_fingerprint AS k)
SELECT
  m.expected_inputs AS expected_inputs,
  v.vector_rows AS vector_rows,
  v.distinct_requests AS distinct_requests,
  v.cached_vectors AS cached_vectors,
  v.new_vectors AS new_vectors,
  n.invalid_vectors AS invalid_vectors,
  m.missing_vectors AS missing_vectors,
  v.unexpected_vectors AS unexpected_vectors,
  n.source_vector_changes AS source_vector_changes,
  d.document_rows AS document_rows,
  d.distinct_documents AS distinct_documents,
  d.document_mapping_errors AS document_mapping_errors,
  u.unused_vectors AS unused_vectors,
  r.raw_output_rows AS raw_output_rows,
  r.raw_distinct_requests AS raw_distinct_requests,
  f.manifest_errors AS manifest_errors,
  IF(v.vector_rows=m.expected_inputs
     AND v.distinct_requests=m.expected_inputs
     AND m.expected_inputs BETWEEN 1 AND 2000000
     AND v.cached_vectors+v.new_vectors=v.vector_rows
     AND n.invalid_vectors=0
     AND m.missing_vectors=0
     AND v.unexpected_vectors=0
     AND n.source_vector_changes=0
     AND d.document_rows=2000000
     AND d.distinct_documents=2000000
     AND d.document_mapping_errors=0
     AND u.unused_vectors=0
     AND r.raw_output_rows=v.new_vectors
     AND r.raw_distinct_requests=v.new_vectors
     AND f.manifest_errors=0, 0, 1) AS final_contract_errors
FROM vectors AS v
CROSS JOIN norms AS n
CROSS JOIN missing AS m
CROSS JOIN documents AS d
CROSS JOIN unused AS u
CROSS JOIN raw_totals AS r
CROSS JOIN manifest AS f
LIMIT 1;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF((final_contract_errors=0) IS DISTINCT
               FROM TRUE)=0
   FROM validation_control) AS 'dense_final_status: frozen contract failed; retain the original job for recovery';


SELECT
  expected_inputs,
  vector_rows,
  distinct_requests,
  cached_vectors,
  new_vectors,
  invalid_vectors,
  missing_vectors,
  unexpected_vectors,
  source_vector_changes,
  document_rows,
  distinct_documents,
  document_mapping_errors,
  unused_vectors,
  raw_output_rows,
  raw_distinct_requests,
  manifest_errors,
  final_contract_errors
FROM validation_control;
